import json
import time
import os
import sys
import shutil
from argparse import ArgumentParser

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader
from torch.utils.data.dataset import Subset

import protein_features
import struct2seq
import data
import utils
import noam_opt



# 0.1 noise
# 0.2*cctop(CRF loss, no smoothing) + recovery(smoothing 0.1)
# CRF Decoder for cctop
# Only I,M,O,L,S 

# Get the input parameters
parser = ArgumentParser(description='Structure to sequence modeling')
parser.add_argument('--temperature', type=float, default=1.0, help='Temperature to sample an amino acid')
parser.add_argument('--noise_2D', type=float, default=0.1, help='Add noise distance map in training')
parser.add_argument('--noise_3D', type=float, default=0.02, help='Add noise to frame in training')
parser.add_argument('--shuffle', type=float, default=0., help='Shuffle fraction')
parser.add_argument('--data_jsonl', type=str,help='Path for the jsonl data')
parser.add_argument('--split_json', type=str, help='Path for the split json file')
parser.add_argument('--output_folder',type=str,default="output/",help="output folder for the log files and model parameters")
parser.add_argument('--description',type=str,help="description the model information into wandb")
parser.add_argument('--job_name',type=str,default="model_zb",help="jobname of the wandb dashboard")
parser.add_argument('--num_tags',type=int,default=5,help="num tags for the sequence")
parser.add_argument('--epochs',type=int,default=500,help="epochs to train the model")
parser.add_argument('--batch_size',type=int,default=8000,help="batch size tokens")
parser.add_argument('--mask',type=float,default=1.0,help="mask fractions into input sequences")
parser.add_argument('--max_length',type=int,default=1300,help="max length of the training sequence")
parser.add_argument('--ipa_layer',type=int,default=3,help="ipa layers in the middle blocks")
parser.add_argument('--encoder_layer',type=int,default=3,help="encoder layers")
parser.add_argument('--decoder_layer',type=int,default=3,help="decoder layers")





args = parser.parse_args()

if not os.path.exists(args.output_folder):
    os.makedirs(args.output_folder)
if not os.path.exists(os.path.join(args.output_folder,"checkpoints")):
    os.makedirs(os.path.join(args.output_folder,"checkpoints"))

# Load the data
print("start loading parameters...")
jsonl_file = args.data_jsonl
split_file = args.split_json
dataset = data.StructureDataset(jsonl_file=jsonl_file, max_length=args.max_length,high_fraction=args.mask) # total dataset of the pdb files
# Split the dataset

dataset_indices = {d['name']:i for i,d in enumerate(dataset)} # 每个名字对应idx
with open(f"{split_file}","r") as f:
    dataset_splits = json.load(f)
train_set, validation_set, test_set = [
    Subset(dataset, [
        dataset_indices[chain_name] for chain_name in dataset_splits[key]
        if chain_name in dataset_indices
    ])
    for key in ['train', 'validation', 'test']
] # 对于train样本,for chain_name in dataset_splits[key]找到所有train的Pdb名字, dataset_indices[chain_name]找到该名字对应的dataset idx



loader_train, loader_validation, loader_test = [data.StructureTokenloader(d, batch_size=args.batch_size) for d in [train_set, validation_set, test_set]]

with open(os.path.join(args.output_folder,"log_all.txt"),"a") as f:
    f.write(f'Training:{len(train_set)}, Validation:{len(validation_set)}, Test:{len(test_set)}\n')
# print(f'Training:{len(train_set)}, Validation:{len(validation_set)}, Test:{len(test_set)}')

# Log files
logfile = os.path.join(args.output_folder,"log.txt")
with open(logfile,"w") as f:
    f.write("Epoch,Train,Validation\n")
# Training Epochs (Training + Validation + Save model)
start_train = time.time()
epoch_losses_train, epoch_losses_valid = [], []
epoch_checkpoints = []
total_step = 0

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
model = struct2seq.TMPNN(device=device,noise_2D=args.noise_2D,noise_3D=args.noise_3D,ipa_layer=args.ipa_layer,num_tags=args.num_tags,num_encoder_layers=args.encoder_layer,num_decoder_layers=args.decoder_layer)
model = model.to(device)
optimizer,schuduler = noam_opt.transformer_optim_setup(model.parameters(),128)

start_time = time.time()

print("start training...")
import wandb
# from torch.utils.tensorboard import SummaryWriter
# writer = SummaryWriter()
# wandb.init(project='tmpnn-ipa-v2.0', sync_tensorboard=True)
wandb.init(project=args.job_name)
wandb.config = {
  "epochs": args.epochs,
  "batch_size": args.batch_size,
  "mask":args.mask,
  "noise_2D":args.noise_2D,
  "noise_3D":args.noise_3D,
  "describe":args.description,
  "num_tags":args.num_tags,
  "ipa_layer":args.ipa_layer,
  "encoder_layer":args.encoder_layer,
  "decoder_layer":args.decoder_layer
}
for e in range(args.epochs):
    # Training epoch
    model.train()
    train_sum, train_weights = 0., 0.
    cctop_train_sum = 0.
    for train_i, batch in enumerate(loader_train):
        start_batch = time.time()
        # Get a batch, S_mask for the encoder module
        for key in batch.keys():
            batch[key] = batch[key].to(device)
        X = batch["coord"]
        S = batch["seq"]
        mask = batch["mask"]
        C = batch["cctop"]
        lengths = batch["length"]
        S_mask = batch["mask_seq"]
        num_tokens = (torch.sum(lengths)).item()

        optimizer.zero_grad()
        log_probs_seq, logits_cctop = model(X, S, S_mask, lengths, mask,device=device)
        _, loss_av_smoothed = utils.loss_smoothed(S, log_probs_seq, mask, weight=0.05,num_classes=22)
        loss_crf = model.neg_loss_crf(logits_cctop,C,mask)
        # _, cctop_loss_av_smoothed = utils.loss_smoothed(C, log_probs_cctop, mask, weight=0.01,num_classes=5)
        loss_bw = 0.2 * loss_crf + loss_av_smoothed
        loss_bw.backward()
        optimizer.step()
        schuduler.step()
        lr = schuduler.get_last_lr()[0]

        # writer.add_scalar('Loss', loss_bw, total_step)
        

        loss, loss_av = utils.loss_nll(S, log_probs_seq, mask)
        # crf decoder output List[List[int]] not a tensor, add the mask tensor
        bag_list = model.decode_crf(logits_cctop,mask)
        for i in range(len(bag_list)):
            if len(bag_list[i]) != S.size(1):
                bag_list[i] += [0 for _ in range(S.size(1)-len(bag_list[i]))]
        cctop_train = torch.tensor(bag_list,dtype=torch.long,device=device)
        acc_cctop_train = torch.sum((cctop_train == C) * mask)
        cctop_train_sum += acc_cctop_train

        total_step += 1
        with open(os.path.join(args.output_folder,"log_all.txt"),"a") as f:
            f.write(f"|\tEpoch {e}\t|\tIteration {train_i}\t|\tPPL {np.exp(loss_av.cpu().data.numpy()) :.3f}\t|\tPPL_sm {np.exp(loss_av_smoothed.cpu().data.numpy()) :.3f}|\tAcc{acc_cctop_train/num_tokens :.4f}|\n")
        
        # tensorboard visualization - for training
        # writer.add_scalar('PPL/train', np.exp(loss_av_smoothed.cpu().data.numpy()), total_step)
        # writer.add_scalar('Acc/train', acc_cctop_train/num_tokens, total_step)

        wandb.log({'Loss': loss_bw, 'PPL': np.exp(loss_av_smoothed.cpu().data.numpy()),"Acc/train": acc_cctop_train/num_tokens ,"lr":lr})

        # Accumulate true loss
        train_sum += torch.sum(loss * mask).cpu().data.numpy()
        train_sum += (loss_crf.cpu().data.numpy()) * num_tokens
        train_weights += torch.sum(mask).cpu().data.numpy()

    # Validation epoch
    model.eval()
    with torch.no_grad():
        validation_sum, validation_weights = 0., 0.
        validation_sum_cctop = 0.
        for _, batch in enumerate(loader_validation):
            # Get a batch, S_mask for the encoder module
            for key in batch.keys():
                batch[key] = batch[key].to(device)
            X = batch["coord"]
            S = batch["seq"]
            mask = batch["mask"]
            C = batch["cctop"]
            lengths = batch["length"]
            S_mask = batch["mask_seq"]
            num_tokens = (torch.sum(lengths)).item()


            log_probs_seq, logits_cctop = model(X, S, S_mask, lengths, mask,device=device)
            loss, loss_av = utils.loss_nll(S, log_probs_seq, mask)
            loss_crf = model.neg_loss_crf(logits_cctop,C,mask)

            bag_list = model.decode_crf(logits_cctop,mask)
            for i in range(len(bag_list)):
                if len(bag_list[i]) != S.size(1):
                    bag_list[i] += [0 for _ in range(S.size(1)-len(bag_list[i]))]
            cctop_validation = torch.tensor(bag_list,dtype=torch.long,device=device)
            acc_cctop_validation = torch.sum((cctop_validation == C) * mask)
            validation_sum_cctop += acc_cctop_validation
            # Accumulate
            validation_sum += torch.sum(loss * mask).cpu().data.numpy()
            validation_sum += loss_crf.cpu().data.numpy() *num_tokens
            validation_weights += torch.sum(mask).cpu().data.numpy()

    train_loss = train_sum / train_weights
    train_perplexity = np.exp(train_loss)
    train_cctop = cctop_train_sum/train_weights
    validation_loss = validation_sum / validation_weights
    validation_perplexity = np.exp(validation_loss)
    validation_cctop = validation_sum_cctop/validation_weights
    with open(os.path.join(args.output_folder,"log_all.txt"),"a") as f:
        f.write(f"Loss\tTrain {train_loss :.4f}\t\tValidation {validation_loss :.4f}\n")
        f.write(f"Perplexity\tTrain:{train_perplexity :.4f}\t\tValidation:{validation_perplexity :.4f}\n")
        f.write(f"Acc\tTrain:{train_cctop :.4f}\tValidation:{validation_cctop:.4f}\n")
    
    # tensorboard visualization - for training
    # writer.add_scalar('PPL-epoch/train', train_perplexity, e)
    # writer.add_scalar('Acc-epoch/train', train_cctop, e)
    # writer.add_scalar('PPL-epoch/validation', nvalidation_perplexity, e)
    # writer.add_scalar('Acc-epoch/validation', validation_cctop, e)
    wandb.log({'PPL-epoch/train': train_perplexity, 'Acc-epoch/train': train_cctop,"PPL-epoch/validation": validation_perplexity, "Acc-epoch/validation":validation_cctop })


    with open(logfile, 'a') as f:
        f.write(f"{e}\t{train_perplexity}\t{validation_perplexity}\n")

    # Save the model
    checkpoint_filename = os.path.join(args.output_folder ,'checkpoints/epoch{}_step{}.pt'.format(e+1, total_step))
    torch.save({
        'epoch': e,
        'hyperparams': vars(args),
        'model_state_dict': model.state_dict(),
        'optimizer_state_dict': optimizer.state_dict(),
        'scheduler_state_dict':schuduler.state_dict(),
        'step':total_step
    }, checkpoint_filename)

    epoch_losses_valid.append(validation_perplexity)
    epoch_losses_train.append(train_perplexity)
    epoch_checkpoints.append(checkpoint_filename)

# Determine best model via early stopping on validation
best_model_idx = np.argmin(epoch_losses_valid).item()
best_checkpoint = epoch_checkpoints[best_model_idx]
train_perplexity = epoch_losses_train[best_model_idx]
validation_perplexity = epoch_losses_valid[best_model_idx]
best_checkpoint_copy = os.path.join(args.output_folder ,'best_checkpoint_epoch{}.pt'.format(best_model_idx + 1))
shutil.copy(best_checkpoint, best_checkpoint_copy)
utils.load_checkpoint(best_checkpoint_copy, model)


# Test epoch
model.eval()
with torch.no_grad():
    test_sum, test_weights = 0., 0.
    test_sum_cctop=0.
    for _, batch in enumerate(loader_test):
        # Get a batch, S_mask for the encoder module
        for key in batch.keys():
            batch[key] = batch[key].to(device)
        X = batch["coord"]
        S = batch["seq"]
        mask = batch["mask"]
        C = batch["cctop"]
        lengths = batch["length"]
        S_mask = batch["mask_seq"]
        num_tokens = (torch.sum(lengths)).item()
        log_probs_seq, logits_cctop = model(X, S, S_mask, lengths, mask,device=device)
        loss, loss_av = utils.loss_nll(S, log_probs_seq, mask)
        loss_crf = model.neg_loss_crf(logits_cctop,C,mask)
        # Accumulate
        bag_list = model.decode_crf(logits_cctop,mask)
        for i in range(len(bag_list)):
            if len(bag_list[i]) != S.size(1):
                bag_list[i] += [0 for _ in range(S.size(1)-len(bag_list[i]))]
        cctop_test = torch.tensor(bag_list,dtype=torch.long,device=device)
        acc_cctop_test = torch.sum((cctop_test == C) * mask)
        test_sum_cctop += acc_cctop_test
        test_sum += torch.sum(loss * mask).cpu().data.numpy()
        test_sum += loss_crf.cpu().data.numpy() * num_tokens
        test_weights += torch.sum(mask).cpu().data.numpy()

test_loss = test_sum / test_weights
test_perplexity = np.exp(test_loss)
test_cctop = test_sum_cctop / test_weights
with open(os.path.join(args.output_folder,"log_all.txt"),"a") as f:
    f.write(f"Perplexity\tTest:{test_perplexity :.3f}\tAccuracy\t{test_cctop :.3f}\n")
# print('Perplexity\tTest:{}'.format(test_perplexity))

with open(os.path.join(args.output_folder,"result.txt"), 'w') as f:
    f.write(f'Best epoch: {best_model_idx+1}\nPerplexities:\n\tTrain: {train_perplexity}\n\tValidation: {validation_perplexity}\n\tTest: {test_perplexity},{test_cctop}')
