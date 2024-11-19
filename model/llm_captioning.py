import os
from typing import Any, Dict
import torch
import pytorch_lightning as pl
from torch import optim
from lavis.common.optims import LinearWarmupCosineLRScheduler, LinearWarmupStepLRScheduler
import json
import torch.distributed as dist
from transformers import AutoTokenizer, OPTForCausalLM
from model.help_funcs import caption_evaluate, AttrDict
from opendelta import LoraModel
from opendelta.delta_models.lora import LoraConfig
from model.help_funcs import hf_enable_gradient_checkpointing
from model.blip2_stage2 import evaluate_exact_match
try:
    from model.opt_flash_attention import replace_opt_attn_with_flash_attn, replace_opt_attn_with_original_attn
except ModuleNotFoundError:
    pass

class LLMCaptioning(pl.LightningModule):
    def on_save_checkpoint(self, checkpoint: Dict[str, Any]) -> None:
        # checkpoint.pop('optimizer_states')
        to_be_removed = []
        for key, value in checkpoint['state_dict'].items():
            try:
                if not self.get_parameter(key).requires_grad:
                    to_be_removed.append(key)
            except AttributeError:
                to_be_removed.append(key)
        for key in to_be_removed:
            checkpoint['state_dict'].pop(key)
    
    def __init__(self, args):
        super().__init__()
        if isinstance(args, dict):
            args = AttrDict(**args)
        self.args = args
        self.caption_eval_epoch = args.caption_eval_epoch
        self.do_sample = args.do_sample
        self.num_beams = args.num_beams
        self.max_inference_len = args.max_inference_len
        self.min_inference_len = args.min_inference_len
        self.llm_tune = args.llm_tune
        self.llm_name = args.llm_name
        self.enable_flash = args.enable_flash

        
        ## initialize opt model
        self.tokenizer = AutoTokenizer.from_pretrained(self.llm_name, use_fast=False, padding_side='right')
        self.tokenizer.add_special_tokens({'pad_token': '<pad>'})
        
        self.llm_model = OPTForCausalLM.from_pretrained(self.llm_name, torch_dtype=torch.bfloat16)
        self.llm_model.resize_token_embeddings(len(self.tokenizer)) # for the special placeholder token
        
        if args.enbale_gradient_checkpointing:
            self.llm_model = hf_enable_gradient_checkpointing(self.llm_model)
        if self.llm_tune == 'freeze':
            for name, param in self.llm_model.named_parameters():
                param.requires_grad = False
        elif self.llm_tune == 'full':
            for name, param in self.llm_model.named_parameters():
                param.requires_grad = True
        elif self.llm_tune == 'lora':
            lora_config = LoraConfig(args.lora_r, args.lora_alpha, args.lora_dropout)
            self.delta = LoraModel.from_config(lora_config, self.llm_model)
            self.delta.freeze_module(set_state_dict=False)
            self.delta.log()
        elif self.llm_tune == 'mid_lora':
            lora_config = LoraConfig(args.lora_r, args.lora_alpha, args.lora_dropout, modified_modules=["q_proj", "v_proj", 'k_proj', "out_proj", "fc1", "fc2"])
            self.delta = LoraModel.from_config(lora_config, self.llm_model)
            self.delta.freeze_module(set_state_dict=False)
            self.delta.log()
        else:
            raise NotImplementedError()

        ## fixme: this is different from the original BLIP2
        self.eos_token_id = self.tokenizer(
            "\n", add_special_tokens=False
        ).input_ids[0]
        self.save_hyperparameters(args)
    
    def configure_optimizers(self):
        self.trainer.fit_loop.setup_data()
        warmup_steps = min(len(self.trainer.train_dataloader), self.args.warmup_steps)
        optimizer = optim.AdamW(self.parameters(), lr=self.args.init_lr, weight_decay=self.args.weight_decay)
        if self.args.scheduler == 'linear_warmup_cosine_lr':
            self.scheduler = LinearWarmupCosineLRScheduler(optimizer, self.args.max_epochs, self.args.min_lr, self.args.init_lr, warmup_steps, self.args.warmup_lr)
        elif self.args.scheduler == 'linear_warmup_step_lr':
            self.scheduler = LinearWarmupStepLRScheduler(optimizer, self.args.max_epochs, self.args.min_lr, self.args.init_lr, self.args.lr_decay_rate, self.args.warmup_lr, warmup_steps)
        elif self.args.scheduler == 'None':
            self.scheduler = None
        else:
            raise NotImplementedError()
        return optimizer
    
    def save_predictions(self, predictions, targets, q_types=None, log_prefix=''):
        assert len(predictions) == len(targets)
        if log_prefix:
            name = f'{log_prefix}_predictions.txt'
        else:
            name = 'predictions.txt'
        with open(os.path.join(self.logger.log_dir, name), 'w', encoding='utf8') as f:
            if q_types is not None:
                for p, t, q in zip(predictions, targets, q_types):
                    line = {'prediction': p, 'target': t, 'q_type': q}
                    f.write(json.dumps(line, ensure_ascii=True) + '\n')
            else:
                for p, t in zip(predictions, targets):
                    line = {'prediction': p, 'target': t}
                    f.write(json.dumps(line, ensure_ascii=True) + '\n')


    def on_validation_epoch_end_old(self):
        if self.enable_flash:
            replace_opt_attn_with_flash_attn()
        if (self.current_epoch+1) % self.caption_eval_epoch != 0:
            return 

        predictions0 = [i for ii in self.prediction_list0 for i in ii]
        targets0 = [i for ii in self.target_list0 for i in ii['answers']]
        if 'q_types' in self.target_list0[0]:
            q_types0 = [i for ii in self.target_list0 for i in ii['q_types']]
            self.reduce_and_evaluate_qa(predictions0, targets0, q_types0, 'dataset0')
        else:
            self.reduce_and_evaluate_captioning(predictions0, targets0, 'dataset0')

        assert len(self.prediction_list1) == 0 ## exlude the second dataset
        if len(self.prediction_list1) > 0:
            predictions1 = [i for ii in self.prediction_list1 for i in ii]
            targets1 = [i for ii in self.target_list1 for i in ii]
            self.reduce_and_evaluate_captioning(predictions1, targets1, 'dataset1')
    
    def reduce_and_evaluate_qa(self, predictions, targets, q_types, log_prefix=""):
        all_predictions = [None for _ in range(self.trainer.world_size)]
        all_targets = [None for _ in range(self.trainer.world_size)]
        all_q_types = [None for _ in range(self.trainer.world_size)]
        dist.all_gather_object(all_predictions, predictions)
        dist.all_gather_object(all_targets, targets)
        dist.all_gather_object(all_q_types, q_types)
        if self.global_rank == 0:
            all_predictions = [i for ii in all_predictions for i in ii]
            all_targets = [i for ii in all_targets for i in ii]
            all_q_types = [i for ii in all_q_types for i in ii]
            self.save_predictions(all_predictions, all_targets, all_q_types, log_prefix=log_prefix)

    def reduce_and_evaluate_captioning(self, predictions, targets, log_prefix=""):
        all_predictions = [None for _ in range(self.trainer.world_size)]
        all_targets = [None for _ in range(self.trainer.world_size)]
        dist.all_gather_object(all_predictions, predictions)
        dist.all_gather_object(all_targets, targets)
        if self.global_rank == 0:
            all_predictions = [i for ii in all_predictions for i in ii]
            all_targets = [i for ii in all_targets for i in ii]
            self.save_predictions(all_predictions, all_targets, log_prefix=log_prefix)
            ## fixme: I am not sure if the max length is the same as previous experiments
            bleu2, bleu4, rouge_1, rouge_2, rouge_l, meteor_score = \
                caption_evaluate(all_predictions, all_targets, self.tokenizer, self.max_inference_len) 
            acc = evaluate_exact_match(all_predictions, all_targets)
            self.log(f"{log_prefix}/acc", acc, sync_dist=False)
            self.log(f"{log_prefix}/bleu2", bleu2, sync_dist=False)
            self.log(f"{log_prefix}/bleu4", bleu4, sync_dist=False)
            self.log(f"{log_prefix}/rouge_1", rouge_1, sync_dist=False)
            self.log(f"{log_prefix}/rouge_2", rouge_2, sync_dist=False)
            self.log(f"{log_prefix}/rouge_l", rouge_l, sync_dist=False)
            self.log(f"{log_prefix}/meteor_score", meteor_score, sync_dist=False)

    def on_validation_epoch_start(self) -> None:
        if self.enable_flash:
            replace_opt_attn_with_original_attn()
        self.saved_dict_list = []
        self.prediction_list0 = []
        self.target_list0 = []
        self.prediction_list1 = []
        self.target_list1 = []
        
    def validation_step(self, batch, batch_idx, dataloader_idx=0):
        if (dataloader_idx % 2) == 0:
            batch_size = batch.input_ids.shape[0]
            loss = self.lm_loss(batch)
            self.log(f"dataloader{dataloader_idx}/val loss", float(loss), batch_size=batch_size, sync_dist=True)
            return loss
        elif (dataloader_idx % 2) == 1:
            if (self.current_epoch+1) % self.caption_eval_epoch != 0:
                return 
            input_batch, target_dict = batch
            samples = {'input_batch': input_batch}
            ###============== Captioning Results ===================###
            predictions = self.generate(
                samples,
                do_sample=self.do_sample,
                num_beams=self.num_beams,
                max_length=self.max_inference_len,
                min_length=self.min_inference_len,
            )
            target_dict['predictions'] = predictions
            self.saved_dict_list.append(target_dict)
    
    def gather_dict_results(self, dict_list):
        list_of_dict_list = [None for _ in range(self.trainer.world_size)]
        dist.all_gather_object(list_of_dict_list, dict_list)
        dict_list = [i for ii in list_of_dict_list for i in ii] ## dict list, each dict has values that are lists of predictions, etc.
        keys = dict_list[0].keys()
        gathered_dict = {} # each value is a list of predictions, etc.
        for key in keys:
            gathered_dict[key] = [i for d in dict_list for i in d[key]]
        dict_list = []
        for i in range(len(gathered_dict['predictions'])):
            d = {gathered_dict[k][i] for k in keys}
            dict_list.append(d)
        return dict_list

    def save_results(self, dict_list, log_prefix=""):
        ## save the results
        if log_prefix:
            name = f'{log_prefix}_predictions.txt'
        else:
            name = 'predictions.txt'
        keys = dict_list[0].keys()
        with open(os.path.join(self.logger.log_dir, name), 'w', encoding='utf8') as f:
            for i in range(len(dict_list['predictions'])):
                line = {k: None for k in keys}
                for key in keys:
                    line[key] = dict_list[key][i]
                f.write(json.dumps(line, ensure_ascii=True) + '\n')

    def on_validation_epoch_end(self):
        if self.enable_flash:
            replace_opt_attn_with_flash_attn()
        if (self.current_epoch+1) % self.caption_eval_epoch != 0:
            return 
        result_list = self.gather_dict_results(self.saved_dict_list)
        ## empty cache
        self.saved_dict_list = []
        
        if self.global_rank == 0:
            self.save_results(result_list, 'dataset0')
            all_predictions = [i['predictions'] for i in result_list]
            all_targets = [i['targets'] for i in result_list]
            
            log_prefix = 'dataset0' ## fixme: this is just a placeholder
            if 'q_types' in result_list[0]:
                ## evaluate protein qa
                pass
            else:
                ## evaluate captioning
                bleu2, bleu4, rouge_1, rouge_2, rouge_l, meteor_score = \
                    caption_evaluate(all_predictions, all_targets, self.blip2.llm_tokenizer, self.max_inference_len) 
                acc = evaluate_exact_match(all_predictions, all_targets)
                self.log(f"{log_prefix}/acc", acc, sync_dist=False)
                self.log(f"{log_prefix}/bleu2", bleu2, sync_dist=False)
                self.log(f"{log_prefix}/bleu4", bleu4, sync_dist=False)
                self.log(f"{log_prefix}/rouge_1", rouge_1, sync_dist=False)
                self.log(f"{log_prefix}/rouge_2", rouge_2, sync_dist=False)
                self.log(f"{log_prefix}/rouge_l", rouge_l, sync_dist=False)
                self.log(f"{log_prefix}/meteor_score", meteor_score, sync_dist=False)
        
    
    @torch.no_grad()
    def validation_step_old(self, batch, batch_idx, dataloader_idx=0):
        if (dataloader_idx % 2) == 0:
            if False:
                input_batch, text_batch = batch
                batch_size = input_batch.input_ids.shape[0]
            else:
                batch_size = batch.input_ids.shape[0]
            loss = self.lm_loss(batch)
            self.log(f"dataloader{dataloader_idx}/val loss", float(loss), batch_size=batch_size, sync_dist=True)
            return loss
        elif (dataloader_idx % 2) == 1:
            if (self.current_epoch+1) % self.caption_eval_epoch != 0:
                return 
            input_batch, target_dict = batch
            samples = {'input_batch': input_batch}
            ###============== Captioning Results ===================###
            predictions = self.generate(
                samples,
                do_sample=self.do_sample,
                num_beams=self.num_beams,
                max_length=self.max_inference_len,
                min_length=self.min_inference_len,
            )
            if dataloader_idx // 2 == 0:
                self.prediction_list0.append(predictions)
                self.target_list0.append(target_dict)
            elif dataloader_idx // 2 == 1:
                self.prediction_list1.append(predictions)
                self.target_list1.append(target_dict)
            else:
                raise NotImplementedError
        else:
            raise NotImplementedError
        
    @torch.no_grad()
    def generate(
        self, 
        samples,
        do_sample=False,
        num_beams=5,
        max_length=128,
        min_length=1,
        top_p=0.9,
        repetition_penalty=1.0,
        length_penalty=1.0,
        num_captions=1,
        temperature=1
        ):
        input_batch = samples['input_batch']
        inputs_embeds = self.llm_model.get_input_embeddings()(input_batch.input_ids)
        outputs = self.llm_model.generate(
            inputs_embeds=inputs_embeds,
            attention_mask=input_batch.attention_mask,
            do_sample=do_sample,
            top_p=top_p,
            temperature=temperature,
            num_beams=num_beams,
            max_length=max_length,
            min_length=min_length,
            eos_token_id=self.eos_token_id,
            repetition_penalty=repetition_penalty,
            length_penalty=length_penalty,
            num_return_sequences=num_captions,
        )
        output_text = self.tokenizer.batch_decode(outputs, skip_special_tokens=True)
        output_text = [text.strip() for text in output_text]
        return output_text

    def training_step(self, batch, batch_idx):
        if self.scheduler:
            self.scheduler.step(self.trainer.current_epoch, self.trainer.global_step)
        
        if False:
            prot_batch, text_batch = batch
            batch_size = prot_batch.input_ids.shape[0]
        else:
            batch_size = batch.input_ids.shape[0]
        loss = self.lm_loss(batch)
        self.log('train_loss', float(loss), batch_size=batch_size, sync_dist=True)
        return {"loss": loss}

    def lm_loss(self, batch):
        targets = batch.input_ids.masked_fill(batch.input_ids == self.tokenizer.pad_token_id, -100)
        targets = targets.masked_fill(batch.token_type_ids == 0, -100)
        outputs = self.llm_model(
            input_ids=batch.input_ids,
            attention_mask=batch.attention_mask,
            return_dict=True,
            labels=targets,
        )
        loss = outputs.loss
        return loss
    
    def lm_loss_v2(self, batch):
        ## note the prot_batch contains the prompt already
        prot_batch, text_batch = batch
        device = prot_batch.input_ids.device

        attention_mask = torch.cat((prot_batch.attention_mask, text_batch.attention_mask), dim=1)
        empty_targets = torch.ones(prot_batch.attention_mask.size(), dtype=torch.long).to(device).fill_(-100)
        targets = text_batch.input_ids.masked_fill(
            text_batch.input_ids == self.tokenizer.pad_token_id, -100
        )
        targets = torch.cat([empty_targets, targets], dim=1)
        input_ids = torch.cat((prot_batch.input_ids, text_batch.input_ids), dim=1)
        outputs = self.llm_model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            return_dict=True,
            labels=targets,
        )
        loss = outputs.loss
        return loss
    
    def training_stepv2(self, batch, batch_idx):
        if self.scheduler:
            self.scheduler.step(self.trainer.current_epoch, self.trainer.global_step)
        
        ## note the prot_batch contains the prompt already
        prot_batch, text_batch = batch
        batch_size = prot_batch.input_ids.shape[0]

        ## encode prefix
        prefix_output = self.llm_model.model(
            input_ids=prot_batch.input_ids,
            attention_mask=prot_batch.attention_mask,
            use_cache=True,
            return_dict=True,
        )
        
        attention_mask = torch.cat((prot_batch.attention_mask, text_batch.attention_mask), dim=1)
        targets = text_batch.input_ids.masked_fill(
            text_batch.input_ids == self.tokenizer.pad_token_id, -100
        )
        outputs = self.llm_model(
            input_ids=text_batch.input_ids,
            attention_mask=attention_mask,
            past_key_values=prefix_output.past_key_values,
            return_dict=True,
            labels=targets,
        )
        loss = outputs.loss
        self.log('train_loss', float(loss), batch_size=batch_size, sync_dist=True)
        return {"loss": loss}
    
    @staticmethod
    def add_model_specific_args(parent_parser):
        parser = parent_parser.add_argument_group("")
        # train mode
        # OPT
        parser.add_argument('--llm_name', type=str, default="facebook/galactica-1.3b")
        parser.add_argument('--num_beams', type=int, default=5)
        parser.add_argument('--do_sample', action='store_true', default=False)
        parser.add_argument('--max_inference_len', type=int, default=128)
        parser.add_argument('--min_inference_len', type=int, default=1)
        parser.add_argument('--llm_tune', type=str, default='freeze')
        parser.add_argument('--peft_dir', type=str, default='')
        parser.add_argument('--save_every_n_epochs', type=int, default=0)
        ## lora config
        parser.add_argument('--lora_r', type=int, default=8)
        parser.add_argument('--lora_alpha', type=int, default=32)
        parser.add_argument('--lora_dropout', type=int, default=0.1)
        parser.add_argument('--peft_config', type=str, default=None)
        parser.add_argument('--enbale_gradient_checkpointing', action='store_true', default=False)

        # optimization
        parser.add_argument('--reaction_weight', type=float, default=1.0)
        parser.add_argument('--weight_decay', type=float, default=0.05, help='optimizer weight decay')
        parser.add_argument('--init_lr', type=float, default=1e-4, help='optimizer init learning rate')
        parser.add_argument('--min_lr', type=float, default=1e-5, help='optimizer min learning rate')
        parser.add_argument('--warmup_lr', type=float, default=1e-6, help='optimizer warmup learning rate')
        parser.add_argument('--warmup_steps', type=int, default=1000, help='optimizer warmup steps')
        parser.add_argument('--lr_decay_rate', type=float, default=0.9, help='optimizer lr decay rate')
        parser.add_argument('--scheduler', type=str, default='linear_warmup_cosine_lr', help='type of scheduler') # or linear_warmup_step_lr
        parser.add_argument('--init_checkpoint', type=str, default='')
        parser.add_argument('--caption_eval_epoch', type=int, default=10)
        return parent_parser

