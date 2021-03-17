#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Python version: 3.6


import os
import copy
import time
import pickle
import numpy as np
from tqdm import tqdm
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

import torch
from torch.utils.data import DataLoader
from opacus.dp_model_inspector import DPModelInspector

from options import args_parser
from update import LocalUpdate, test_inference
from utils import get_dataset, average_weights, exp_details
from models import fcn_mobilenetv2, deeplabv3_mobilenetv3, convert_batchnorm_modules
from train import train_one_epoch, evaluate, criterion

MAX_GRAD_NORM = 1.2
NOISE_MULTIPLIER = 0.38
DELTA = 1e-5

if __name__ == '__main__':
    args = args_parser()
    log = ['Options:', str(args)]
    def print_log(string, log=log):
        ''' print string and append to log[list] '''
        print(string)
        log.append(string)

    start_time = time.time()
    exp_details(args)
    torch.manual_seed(args.seed)    
    device = 'cuda' if torch.cuda.is_available() and not args.cpu_only else 'cpu'
    print_log('device: ' + device)

    # load dataset and user groups
    train_dataset, test_dataset, user_groups = get_dataset(args)
    test_loader = DataLoader(train_dataset, batch_size=1, num_workers=args.num_workers, shuffle=False)

    # BUILD MODEL
    if args.model == 'fcn_mobilenetv2':
        # Convolutional neural netorks
        global_model = fcn_mobilenetv2(num_classes=args.num_classes, aux_loss=True)
    elif args.model == 'deeplabv3_mobilenetv2':
        global_model = deeplabv3_mobilenetv3(num_classes=args.num_classes, aux_loss=True)
    else:
        exit('Error: unrecognized model')
    # change model architecutre from batch_norm to group_norm for DP
    if args.dp:
        global_model = convert_batchnorm_modules(global_model)
        inspector = DPModelInspector()
        assert inspector.validate(global_model) == True            

    # Set the model to train and send it to device.
    global_model.to(device)
    global_model.train()

    # copy weights
    global_weights = global_model.state_dict()

    # Load checkpoint
    start_ep = 0
    if args.checkpoint is not None:
        checkpoint = torch.load(
            os.path.join( args.root, 'save/checkpoints', args.checkpoint),
            map_location=device)
        global_model.load_state_dict(checkpoint['model'])
        start_ep = checkpoint['epoch'] + 1 

    # Global rounds / Training
    print_log('\nTraining global model on {} of {} users locally for {} epochs'.format(args.frac, args.num_users, args.epochs))
    train_loss, local_test_accuracy, local_test_iou = [], [], []
    print_every = 1
    for epoch in tqdm(range(start_ep, args.epochs)):
        local_weights, local_losses = [], []
        print_log('\n | Global Training Round : {} |\n'.format(epoch+1))

        global_model.train()
        m = max(int(args.frac * args.num_users), 1)
        idxs_users = np.random.choice(range(args.num_users), m, replace=False)
        # Local training
        for idx in idxs_users:
            print_log('\nUser idx : ' + str(idx))

            local_model = LocalUpdate(args=args, dataset=train_dataset,
                                      idxs=user_groups[idx])
            w, loss = local_model.update_weights(model=copy.deepcopy(global_model),
                                                global_round=epoch, log=log)
            local_weights.append(copy.deepcopy(w))
            local_losses.append(copy.deepcopy(loss))

        # update global weights
        print_log('\nWeight averaging')
        global_weights = average_weights(local_weights)
        # update global weights
        global_model.load_state_dict(global_weights)
        # save global model to checkpoint
        exp_name = 'fed_{}_{}_c{}_e{}_C[{}]_iid[{}]_uneq[{}]_E[{}]_B[{}]_lr[{}x{}]_{}_{}'.\
                    format(args.data, args.model, args.num_classes, epoch+1, args.frac, args.iid, \
                        args.unequal, args.local_ep, args.local_bs, args.lr, args.aux_lr_param, args.lr_scheduler, args.optimizer)
        if args.dp:
            exp_name = 'fedDP_{}_{}_c{}_e{}_C[{}]_iid[{}]_uneq[{}]_E[{}]_B[{}]_lr{}'.\
                    format(args.data, args.model, args.num_classes, epoch+1, args.frac, args.iid, \
                        args.unequal, args.local_ep, args.local_bs, args.lr)                   
        if epoch % args.save_frequency == 0 or epoch == args.epochs-1:
            torch.save(
                {
                    'model': global_model.state_dict(),
                    'epoch': epoch,
                    'exp_name': exp_name
                },
                os.path.join(args.root, 'save/checkpoints', exp_name+'.pth')
            )
        print_log('Global model weights save to checkpoint')

        loss_avg = sum(local_losses) / len(local_losses)
        train_loss.append(loss_avg)

        # Calculate avg test accuracy over all users at every epoch
        last_time = time.time()
        test_users = int(args.local_test_frac * args.num_users)
        print_log('Testing global model on {} users'.format(test_users))
        list_acc, list_iou = [], []
        global_model.eval()
        
        for c in tqdm(range(test_users)):
            local_model = LocalUpdate(args=args, dataset=train_dataset,
                                      idxs=user_groups[idx])            
            acc, iou = local_model.inference(model=global_model)
            list_acc.append(acc)
            list_iou.append(iou)
        local_test_accuracy.append(sum(list_acc)/len(list_acc))
        local_test_iou.append(sum(list_iou)/len(list_iou))

        # print global training loss after every 'i' rounds
        if (epoch+1) % print_every == 0:
            print_log('\nAvg Training Stats after {} global rounds:'.format(epoch+1))
            print_log('Training Loss : {}'.format(np.mean(np.array(train_loss))))
            print_log('Local Test Accuracy: {:.2f}% '.format(local_test_accuracy[-1]))
            print_log('Local Test IoU: {:.2f}%'.format(local_test_iou[-1]))
            print_log('Run Time: {0:0.4f}\n'.format((time.time()-last_time)//60))


    # Inference on test dataset after completion of training
    if not args.train_only:
        print_log('\nTesting global model on global test dataset')
        test_acc, test_iou, confmat = test_inference(args, global_model, test_loader)
        print_log(confmat)
        print_log('\nResults after {} global rounds of training:'.format(args.epochs))
        print_log("|---- Global Test Accuracy: {:.2f}%".format(test_acc))
        print_log("|---- Global Test IoU: {:.2f}%".format(test_iou))
        print_log('\n Total Run Time: {0:0.4f}'.format((time.time()-start_time)//60))

    # Plot Loss curve
    if args.epochs > 1:
        plt.figure()
        plt.title('Training Loss vs Communication rounds')
        plt.plot(range(len(train_loss)), train_loss, color='r')
        plt.ylabel('Training loss')
        plt.xlabel('Communication Rounds')
        plt.savefig(os.path.join(args.root, 'save/training_curves', exp_name+'_loss.png'))

        # Plot Average Accuracy vs Communication rounds
        plt.figure()
        plt.title('Average Accuracy vs Communication rounds')
        plt.plot(range(len(local_test_accuracy)), local_test_accuracy, color='k', label='local test accuracy')
        plt.plot(range(len(local_test_iou)), local_test_iou, color='b', label='local test IoU')
        plt.ylabel('Average Accuracy')
        plt.xlabel('Communication Rounds')
        plt.legend()
        plt.savefig(os.path.join(args.root, 'save/training_curves', exp_name+'_metrics.png'))

    # Logging
    filename = os.path.join(args.root, 'save/logs', exp_name+'_log.txt')
    with open(filename, 'w') as w:
        for line in log:
            w.write(line + '\n')