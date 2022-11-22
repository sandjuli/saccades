"""Train simple models on data synthesized from toy model."""
import os
import argparse
from itertools import product
import numpy as np
from matplotlib import pyplot as plt
import pandas as pd
import seaborn as sns
import torch
from torch import nn
from torch.utils.data import TensorDataset, DataLoader
from torch.optim import SGD
from torch.optim.lr_scheduler import StepLR
from prettytable import PrettyTable
import models_toy as mod
from models import ConvNet
import toy_model_data as toy
from count_unique import Timer
import tetris as pseudo
import numeric
# from labml_nn.hypernetworks.hyper_lstm import HyperLSTM
# from hypernet import HyperLSTM
# torch.set_num_threads(15)

mom = 0.9
wd = 0
start_lr = 0.1
BATCH_SIZE = 500

device = torch.device("cuda")
# device = torch.device("cpu")
criterion = nn.CrossEntropyLoss()
criterion_noreduce = nn.CrossEntropyLoss(reduction='none')
criterion_mse = nn.MSELoss()
criterion_mse_noreduce = nn.MSELoss(reduction='none')


def train_model(rnn, optimizer, scheduler, loaders, config):
    base_name = config.base_name
    avg_num_objects = config.max_num - ((config.max_num-config.min_num)/2)
    n_locs = config.grid**2
    weight_full = (n_locs - avg_num_objects)/ (avg_num_objects+2) # 9 for 9 locations
    weight_count = (n_locs - avg_num_objects)/ avg_num_objects
    pos_weight_count = torch.ones([n_locs], device=device) * weight_count
    pos_weight_full = torch.ones([n_locs], device=device) * weight_full
    criterion_bce_full = nn.BCEWithLogitsLoss(pos_weight=pos_weight_full)
    criterion_bce_count = nn.BCEWithLogitsLoss(pos_weight=pos_weight_count)
    criterion_bce_full_noreduce = nn.BCEWithLogitsLoss(pos_weight=pos_weight_full, reduction='none')
    criterion_bce_count_noreduce = nn.BCEWithLogitsLoss(pos_weight=pos_weight_count, reduction='none')
    n_glimpses = config.max_num
    n_epochs = config.n_epochs
    recurrent_iterations = config.n_iters
    cross_entropy = config.cross_entropy
    nonsymbolic = True if config.shape_input == 'parametric' else False
    learn_shape = config.learn_shape
    train_loader, test_loaders = loaders
    train_loss = np.zeros((n_epochs,))
    # train_map_loss = np.zeros((n_epochs,))
    train_full_map_loss = np.zeros((n_epochs,))
    train_count_map_loss = np.zeros((n_epochs,))
    train_num_loss = np.zeros((n_epochs,))
    train_sh_loss = np.zeros((n_epochs,))
    train_acc = np.zeros((n_epochs,))
    train_shape_loss = np.zeros((n_epochs,))
    n_test_sets = len(config.test_shapes) * len(config.lum_sets)
    test_loss = [np.zeros((n_epochs,)) for _ in range(n_test_sets)]
    # test_map_loss = [np.zeros((n_epochs,)) for _ in range(n_test_sets)]
    test_full_map_loss = [np.zeros((n_epochs,)) for _ in range(n_test_sets)]
    test_count_map_loss = [np.zeros((n_epochs,)) for _ in range(n_test_sets)]
    test_num_loss = [np.zeros((n_epochs,)) for _ in range(n_test_sets)]
    test_sh_loss = [np.zeros((n_epochs,)) for _ in range(n_test_sets)]
    test_acc = [np.zeros((n_epochs,)) for _ in range(n_test_sets)]
    columns = ['pass count', 'correct', 'predicted', 'true', 'loss', 'num loss', 'full map loss', 'count map loss', 'shape loss', 'epoch', 'train shapes', 'test shapes']

    def train_nosymbol(loader):
        rnn.train()
        correct = 0
        epoch_loss = 0
        num_epoch_loss = 0
        map_epoch_loss = 0
        shape_epoch_loss = 0
        for i, (xy, shape, target, locations, shape_label, _) in enumerate(loader):
            rnn.zero_grad()
            input_dim, seq_len = xy.shape[0], xy.shape[1]
            new_order = torch.randperm(seq_len)
            for i in range(input_dim):
                xy[i, :, :] = xy[i, new_order, :]
                shape[i, :, :, :] = shape[i, new_order, :, :]

            hidden = rnn.initHidden(input_dim)
            hidden = hidden.to(device)
            for _ in range(recurrent_iterations):
                for t in range(n_glimpses):
                    pred_num, map, pred_shape, hidden = rnn(xy[:, t, :], shape[:, t, :, :], hidden)

                    if learn_shape:
                        shape_loss = criterion_mse(pred_shape, shape_label[:, t, :])*10
                        shape_loss.backward(retain_graph=True)
                        shape_epoch_loss += shape_loss.item()
            # Calculate lossees
            if cross_entropy:
                num_loss = criterion(pred_num, target)
                pred = pred_num.argmax(dim=1, keepdim=True)
            else:
                num_loss = criterion_mse(torch.squeeze(pred_num), target)
                pred = torch.round(pred_num)

            if config.use_loss == 'num':
                loss = num_loss
                map_loss_to_add = -1
            elif config.use_loss == 'map':
                map_loss = criterion_bce(map, locations)
                map_loss_to_add = map_loss.item()
                loss = map_loss
            elif config.use_loss == 'both':
                map_loss = criterion_bce(map, locations)
                map_loss_to_add = map_loss.item()
                loss = num_loss + map_loss

            loss.backward()
            nn.utils.clip_grad_norm_(rnn.parameters(), 2)
            optimizer.step()

            correct += pred.eq(target.view_as(pred)).sum().item()
            epoch_loss += loss.item()
            num_epoch_loss += num_loss.item()
            map_epoch_loss += map_loss_to_add
        scheduler.step()
        accuracy = 100. * (correct/len(loader.dataset))
        epoch_loss /= len(loader)
        num_epoch_loss /= len(loader)
        map_epoch_loss /= len(loader)
        shape_epoch_loss /= (len(loader) * n_glimpses)
        return epoch_loss, num_epoch_loss, accuracy, map_epoch_loss, shape_epoch_loss

    def train(loader, ep):
        rnn.train()
        correct = 0
        epoch_loss = 0
        num_epoch_loss = 0
        full_map_epoch_loss = 0
        shape_epoch_loss = 0
        no_shuffle = ['recurrent_control', 'cnn']
        for i, (input, target, locations, shape_label, _) in enumerate(loader):
            # if type(locations) is tuple:
            #     (all_loc, count_loc) = locations
            # else:
            #     count_loc = locations
            if config.model_type not in no_shuffle:
                seq_len = input.shape[1]
                # Shuffle glimpse order on each batch
                for i, row in enumerate(input):
                    input[i, :, :] = row[torch.randperm(seq_len), :]
            input_dim = input.shape[0]

            rnn.zero_grad()
            if 'cnn' not in config.model_type:
                hidden = rnn.initHidden(input_dim)
                if config.model_type is not 'hyper':
                    hidden = hidden.to(device)
            # data = toy.generate_dataset(BATCH_SIZE)
            # # data.loc[0]
            # xy = torch.tensor(data['xy']).float()
            # shape = torch.tensor(data['shape']).float()
            # target = torch.tensor(data['numerosity']).long()
            if 'cnn' in config.model_type:
                pred_num, map = rnn(input)
            else:
                for _ in range(recurrent_iterations):
                    for t in range(n_glimpses):
                        if config.model_type == 'recurrent_control':
                            pred_num, map, hidden = rnn(input, hidden)
                        else:
                            pred_num, pred_shape, map, hidden = rnn(input[:, t, :], hidden)
                            if learn_shape:
                                shape_loss = criterion_mse(pred_shape, shape_label[:, t, :])*10
                                shape_epoch_loss += shape_loss.item()
                                shape_loss.backward(retain_graph=True)
                            else:
                                shape_epoch_loss += -1
            if cross_entropy:
                num_loss = criterion(pred_num, target)
                pred = pred_num.argmax(dim=1, keepdim=True)
            else:
                num_loss = criterion_mse(torch.squeeze(pred_num), target)
                pred = torch.round(pred_num)

            def get_map_loss():
                map_loss = criterion_bce_full(map, locations)
                map_loss_to_add = map_loss.item()

                return map_loss, map_loss_to_add

            if config.use_loss == 'num':
                loss = num_loss
                count_map_loss_to_add = -1
                all_map_loss_to_add  = -1
            elif config.use_loss == 'map':
                # map_loss, map_loss_to_add = get_map_loss()
                map_loss, map_loss_to_add = get_map_loss()
                loss = map_loss
            elif config.use_loss == 'both':
                # map_loss, map_loss_to_add = get_map_loss()
                map_loss, map_loss_to_add = get_map_loss()
                loss = num_loss + map_loss
            elif config.use_loss == 'map_then_both':
                # map_loss, map_loss_to_add = get_map_loss()
                map_loss, map_loss_to_add = get_map_loss()
                # map_loss_to_add = count_map_loss
                if ep < 100:
                    loss = map_loss
                else:
                    loss = num_loss + map_loss

            loss.backward()
            nn.utils.clip_grad_norm_(rnn.parameters(), 2)
            optimizer.step()

            correct += pred.eq(target.view_as(pred)).sum().item()
            epoch_loss += loss.item()
            num_epoch_loss += num_loss.item()
            # if not isinstance(map_loss_to_add, int):
                # map_loss_to_add = map_loss_to_add.item()
            full_map_epoch_loss += map_loss_to_add
            # count_map_epoch_loss += count_map_loss_to_add
        scheduler.step()
        accuracy = 100. * (correct/len(loader.dataset))
        epoch_loss /= len(loader)
        num_epoch_loss /= len(loader)
        full_map_epoch_loss /= len(loader)
        # count_map_epoch_loss /= len(loader)
        map_epoch_loss = (full_map_epoch_loss, -1)
        shape_epoch_loss /= len(loader) * n_glimpses
        return epoch_loss, num_epoch_loss, accuracy, shape_epoch_loss, map_epoch_loss

    def train2map(loader, ep):
        rnn.train()
        correct = 0
        epoch_loss = 0
        num_epoch_loss = 0
        full_map_epoch_loss = 0
        count_map_epoch_loss = 0
        shape_epoch_loss = 0
        no_shuffle = ['recurrent_control', 'cnn']
        for i, (input, target, all_loc, count_loc, shape_label, _) in enumerate(loader):
            # if type(locations) is tuple:
            #     (all_loc, count_loc) = locations
            # else:
            #     count_loc = locations
            if config.model_type not in no_shuffle:
                seq_len = input.shape[1]
                # Shuffle glimpse order on each batch
                for i, row in enumerate(input):
                    input[i, :, :] = row[torch.randperm(seq_len), :]
            input_dim = input.shape[0]

            rnn.zero_grad()
            if 'cnn' not in config.model_type:
                hidden = rnn.initHidden(input_dim)
                if config.model_type is not 'hyper':
                    hidden = hidden.to(device)
            # data = toy.generate_dataset(BATCH_SIZE)
            # # data.loc[0]
            # xy = torch.tensor(data['xy']).float()
            # shape = torch.tensor(data['shape']).float()
            # target = torch.tensor(data['numerosity']).long()
            if 'cnn' in config.model_type:
                pred_num, map = rnn(input)
            else:
                for _ in range(recurrent_iterations):
                    for t in range(n_glimpses):
                        if config.model_type == 'recurrent_control':
                            pred_num, map, hidden = rnn(input, hidden)
                        else:
                            pred_num, pred_shape, map, hidden = rnn(input[:, t, :], hidden)
                            if learn_shape:
                                shape_loss = criterion_mse(pred_shape, shape_label[:, t, :])*10
                                shape_epoch_loss += shape_loss.item()
                                shape_loss.backward(retain_graph=True)
                            else:
                                shape_epoch_loss += -1
            if type(map) is tuple:
                (all_map, count_map) = map
            else:
                count_map = map
                all_map = None
            if cross_entropy:
                num_loss = criterion(pred_num, target)
                pred = pred_num.argmax(dim=1, keepdim=True)
            else:
                num_loss = criterion_mse(torch.squeeze(pred_num), target)
                pred = torch.round(pred_num)

            def get_map_loss():
                count_map_loss = criterion_bce_count(count_map, count_loc)
                map_loss_to_add = count_map_loss
                map_loss = count_map_loss
                if all_map is not None:
                    all_map_loss = criterion_bce_full(all_map, all_loc)
                    map_loss += all_map_loss
                return count_map_loss, all_map_loss, map_loss

            if config.use_loss == 'num':
                loss = num_loss
                count_map_loss_to_add = -1
                all_map_loss_to_add  = -1
            elif config.use_loss == 'map':
                # map_loss, map_loss_to_add = get_map_loss()
                count_map_loss, all_map_loss, map_loss = get_map_loss()
                count_map_loss_to_add = count_map_loss.item()
                all_map_loss_to_add = all_map_loss.item()
                loss = map_loss
            elif config.use_loss == 'both':
                # map_loss, map_loss_to_add = get_map_loss()
                count_map_loss, all_map_loss, map_loss = get_map_loss()
                count_map_loss_to_add = count_map_loss.item()
                all_map_loss_to_add = all_map_loss.item()
                loss = num_loss + map_loss
            elif config.use_loss == 'map_then_both':
                # map_loss, map_loss_to_add = get_map_loss()
                count_map_loss, all_map_loss, map_loss = get_map_loss()
                count_map_loss_to_add = count_map_loss.item()
                all_map_loss_to_add = all_map_loss.item()
                # map_loss_to_add = count_map_loss
                if ep < 100:
                    loss = map_loss
                else:
                    loss = num_loss + map_loss

            loss.backward()
            nn.utils.clip_grad_norm_(rnn.parameters(), 2)
            optimizer.step()

            correct += pred.eq(target.view_as(pred)).sum().item()
            epoch_loss += loss.item()
            num_epoch_loss += num_loss.item()
            # if not isinstance(map_loss_to_add, int):
                # map_loss_to_add = map_loss_to_add.item()
            full_map_epoch_loss += all_map_loss_to_add
            count_map_epoch_loss += count_map_loss_to_add
        scheduler.step()
        accuracy = 100. * (correct/len(loader.dataset))
        epoch_loss /= len(loader)
        num_epoch_loss /= len(loader)
        full_map_epoch_loss /= len(loader)
        count_map_epoch_loss /= len(loader)
        map_epoch_loss = (full_map_epoch_loss, count_map_epoch_loss)
        shape_epoch_loss /= len(loader) * n_glimpses
        return epoch_loss, num_epoch_loss, accuracy, shape_epoch_loss, map_epoch_loss

    def test_nosymbol(loader, epoch):
        rnn.eval()
        n_correct = 0
        epoch_loss = 0
        num_epoch_loss = 0
        map_epoch_loss = 0
        test_results = pd.DataFrame(columns=columns)
        for i, (xy, shape, target, locations, _, pass_count) in enumerate(loader):

            input_dim = xy.shape[0]

            batch_results = pd.DataFrame(columns=columns)
            hidden = rnn.initHidden(input_dim)
            hidden = hidden.to(device)

            for _ in range(recurrent_iterations):
                if config.model_type == 'hyper':
                    pred_num, map, hidden = rnn(input, hidden)
                else:
                    for t in range(n_glimpses):
                        pred_num, map, _, hidden = rnn(xy[:, t, :], shape[:, t, :, :], hidden)

            if cross_entropy:
                num_loss = criterion_noreduce(pred_num, target)
                pred = pred_num.argmax(dim=1, keepdim=True)
            else:
                num_loss = criterion_mse_noreduce(torch.squeeze(pred_num), target)
                pred = torch.round(pred_num)

            if config.use_loss == 'num':
                loss = num_loss
                # map_loss = criterion_bce_noreduce(map, locations)
                map_loss_to_add = -1
            elif config.use_loss == 'map':
                map_loss = criterion_bce_noreduce(map, locations)
                map_loss_to_add = map_loss.mean().item()
                loss = map_loss
            elif config.use_loss == 'both':
                map_loss = criterion_bce_noreduce(map, locations)
                map_loss_to_add = map_loss.mean().item()
                loss = num_loss + map_loss.mean()

            correct = pred.eq(target.view_as(pred))
            batch_results['pass count'] = pass_count.detach().cpu().numpy()
            batch_results['correct'] = correct.cpu().numpy()
            batch_results['predicted'] = pred.detach().cpu().numpy()
            batch_results['true'] = target.detach().cpu().numpy()
            batch_results['loss'] = loss.detach().cpu().numpy()
            try:
                batch_results['map loss'] = map_loss.detach().cpu().numpy()
            except:
                batch_results['map loss'] = np.ones(loss.shape) * -1
            batch_results['num loss'] = num_loss.detach().cpu().numpy()
            batch_results['epoch'] = epoch
            test_results = pd.concat((test_results, batch_results))

            n_correct += pred.eq(target.view_as(pred)).sum().item()
            epoch_loss += loss.mean().item()
            num_epoch_loss += num_loss.mean().item()
            map_epoch_loss += map_loss_to_add

        accuracy = 100. * (n_correct/len(loader.dataset))
        epoch_loss /= len(loader)
        num_epoch_loss /= len(loader)
        map_epoch_loss /= len(loader)
        return epoch_loss, num_epoch_loss, accuracy, map_epoch_loss, test_results

    @torch.no_grad()
    def test(loader, epoch):
        rnn.eval()
        n_correct = 0
        epoch_loss = 0
        num_epoch_loss = 0
        full_map_epoch_loss = 0
        # count_map_epoch_loss = 0
        shape_epoch_loss = 0
        nclasses = rnn.output_size
        confusion_matrix = np.zeros((nclasses-config.min_num, nclasses-config.min_num))
        test_results = pd.DataFrame(columns=columns)
        # for i, (input, target, locations, shape_label, pass_count) in enumerate(loader):
        for i, (input, target, all_loc, shape_label, pass_count) in enumerate(loader):
            if nonsymbolic:
                xy, shape = input
                input_dim = xy.shape[0]
            else:
                input_dim = input.shape[0]
            batch_results = pd.DataFrame(columns=columns)
            if 'cnn' not in config.model_type:
                hidden = rnn.initHidden(input_dim)
                if config.model_type != 'hyper':
                    hidden = hidden.to(device)
            # data = toy.generate_dataset(BATCH_SIZE)
            # # data.loc[0]
            # xy = torch.tensor(data['xy']).float()
            # shape = torch.tensor(data['shape']).float()
            # target = torch.tensor(data['numerosity']).long()
            if 'cnn' in config.model_type:
                pred_num, map = rnn(input)
            else:
                for _ in range(recurrent_iterations):
                    if config.model_type == 'hyper':
                        pred_num, map, hidden = rnn(input, hidden)
                    else:
                        for t in range(n_glimpses):
                            if nonsymbolic:
                                pred_num, map, hidden = rnn(xy[:, t, :], shape[:, t, :, :], hidden)
                            elif config.model_type == 'recurrent_control':
                                pred_num, map, hidden = rnn(input, hidden)
                            else:
                                pred_num, pred_shape, map, hidden = rnn(input[:, t, :], hidden)
                                shape_loss = criterion_mse(pred_shape, shape_label[:, t, :])*10
                                shape_epoch_loss += shape_loss.item()

            if cross_entropy:
                num_loss = criterion_noreduce(pred_num, target)
                pred = pred_num.argmax(dim=1, keepdim=True)
            else:
                num_loss = criterion_mse_noreduce(torch.squeeze(pred_num), target)
                pred = torch.round(pred_num)

            def get_map_loss():
                all_map_loss = criterion_bce_full_noreduce(map, all_loc)
                map_loss = all_map_loss.mean(axis=1)
                map_loss_to_add = map_loss.sum().item()
                return map_loss, map_loss_to_add

            if config.use_loss == 'num':
                loss = num_loss
                # map_loss = criterion_bce_noreduce(map, locations)
                map_loss_to_add = -1
                full_map_loss_to_add = -1
                count_map_loss_to_add = -1
            elif config.use_loss == 'map':
                # Average over map locations, sum over instances
                # map_loss = criterion_bce_noreduce(map, locations)
                # map_loss_to_add = map_loss.mean(axis=1).sum()
                # map_loss, map_loss_to_add = get_map_loss()
                # loss = map_loss.mean(axis=1)
                # map_loss, map_loss_to_add = get_map_loss()
                map_loss, map_loss_to_add = get_map_loss()
                loss = map_loss

            elif config.use_loss == 'both':
                # map_loss = criterion_bce_noreduce(map, locations)
                # # map_loss_reduce = criterion_bce(map, locations)
                # map_loss_to_add = map_loss.mean(axis=1).sum()
                # map_loss, map_loss_to_add = get_map_loss()
                map_loss, map_loss_to_add = get_map_loss()
                # loss = num_loss + map_loss.mean(axis=1)
                loss = num_loss + map_loss
            elif config.use_loss == 'map_then_both':
                # map_loss = criterion_bce_noreduce(map, locations)

                # map_loss, map_loss_to_add = get_map_loss()
                map_loss, map_loss_to_add = get_map_loss()

                if ep < 150:
                    loss = map_loss
                else:
                    loss = num_loss + map_loss
            correct = pred.eq(target.view_as(pred))
            batch_results['pass count'] = pass_count.detach().cpu().numpy()
            batch_results['correct'] = correct.cpu().numpy()
            batch_results['predicted'] = pred.detach().cpu().numpy()
            batch_results['true'] = target.detach().cpu().numpy()
            batch_results['loss'] = loss.detach().cpu().numpy()
            try:
                # Somehow before it was like just the first column was going
                # into batch_results, so just map loss for the first out of
                # nine location, instead of the average over all locations.
                # batch_results['map loss'] = map_loss.mean(axis=1).detach().cpu().numpy()
                # batch_results['map loss'] = map_loss.mean(axis=1).detach().cpu().numpy()
                batch_results['full map loss'] = map_loss.detach().cpu().numpy()
                # batch_results['count map loss'] = count_map_loss.detach().cpu().numpy()
            except:
                # batch_results['map loss'] = np.ones(loss.shape) * -1
                batch_results['full map loss'] = np.ones(loss.shape) * -1
                batch_results['count map loss'] = np.ones(loss.shape) * -1
            batch_results['num loss'] = num_loss.detach().cpu().numpy()
            batch_results['shape loss'] = shape_loss.detach().cpu().numpy()
            batch_results['epoch'] = epoch
            test_results = pd.concat((test_results, batch_results))

            n_correct += pred.eq(target.view_as(pred)).sum().item()
            epoch_loss += loss.mean().item()
            num_epoch_loss += num_loss.mean().item()
            # if not isinstance(map_loss_to_add, int):
            #     map_loss_to_add = map_loss_to_add.item()
            full_map_epoch_loss += map_loss_to_add
            # class-specific analysis and confusion matrix
            # c = (pred.squeeze() == target)
            for j in range(target.shape[0]):
                label = target[j]
                confusion_matrix[label-config.min_num, pred[j]-config.min_num] += 1
        # These two lines should be the same
        # map_epoch_loss / len(loader.dataset)
        # test_results['map loss'].mean()

        accuracy = 100. * (n_correct/len(loader.dataset))
        epoch_loss /= len(loader)
        num_epoch_loss /= len(loader)
        # map_epoch_loss /= len(loader)
        if config.use_loss == 'num':
            # map_epoch_loss /= len(loader)
            full_map_epoch_loss /= len(loader)
        else:
            full_map_epoch_loss /= len(loader.dataset)
        map_epoch_loss = (full_map_epoch_loss, -1)
        shape_epoch_loss /= len(loader) * n_glimpses

        return (epoch_loss, num_epoch_loss, accuracy, shape_epoch_loss,
                map_epoch_loss, test_results, confusion_matrix)

    @torch.no_grad()
    def test2map(loader, epoch):
        rnn.eval()
        n_correct = 0
        epoch_loss = 0
        num_epoch_loss = 0
        full_map_epoch_loss = 0
        count_map_epoch_loss = 0
        shape_epoch_loss = 0
        nclasses = rnn.output_size
        confusion_matrix = np.zeros((nclasses-config.min_num, nclasses-config.min_num))
        test_results = pd.DataFrame(columns=columns)
        # for i, (input, target, locations, shape_label, pass_count) in enumerate(loader):
        for i, (input, target, all_loc, count_loc, shape_label, pass_count) in enumerate(loader):
            if nonsymbolic:
                xy, shape = input
                input_dim = xy.shape[0]
            else:
                input_dim = input.shape[0]
            batch_results = pd.DataFrame(columns=columns)
            if 'cnn' not in config.model_type:
                hidden = rnn.initHidden(input_dim)
                if config.model_type != 'hyper':
                    hidden = hidden.to(device)
            # data = toy.generate_dataset(BATCH_SIZE)
            # # data.loc[0]
            # xy = torch.tensor(data['xy']).float()
            # shape = torch.tensor(data['shape']).float()
            # target = torch.tensor(data['numerosity']).long()
            if 'cnn' in config.model_type:
                pred_num, map = rnn(input)
            else:
                for _ in range(recurrent_iterations):
                    if config.model_type == 'hyper':
                        pred_num, map, hidden = rnn(input, hidden)
                    else:
                        for t in range(n_glimpses):
                            if nonsymbolic:
                                pred_num, map, hidden = rnn(xy[:, t, :], shape[:, t, :, :], hidden)
                            elif config.model_type == 'recurrent_control':
                                pred_num, map, hidden = rnn(input, hidden)
                            else:
                                pred_num, pred_shape, map, hidden = rnn(input[:, t, :], hidden)
                                shape_loss = criterion_mse(pred_shape, shape_label[:, t, :])*10
                                shape_epoch_loss += shape_loss.item()
            if type(map) is tuple:
                (all_map, count_map) = map
            else:
                count_map = map
                all_map = None
            if cross_entropy:
                num_loss = criterion_noreduce(pred_num, target)
                pred = pred_num.argmax(dim=1, keepdim=True)
            else:
                num_loss = criterion_mse_noreduce(torch.squeeze(pred_num), target)
                pred = torch.round(pred_num)

            def get_map_loss():
                count_map_loss = criterion_bce_count_noreduce(count_map, count_loc)
                count_map_loss = count_map_loss.mean(axis=1)
                # map_loss_to_add = count_map_loss.mean(axis=1).sum()
                map_loss = count_map_loss
                if all_map is not None:
                    all_map_loss = criterion_bce_full_noreduce(all_map, all_loc)
                    all_map_loss = all_map_loss.mean(axis=1)
                    map_loss += all_map_loss
                return count_map_loss, all_map_loss, map_loss

            if config.use_loss == 'num':
                loss = num_loss
                # map_loss = criterion_bce_noreduce(map, locations)
                map_loss_to_add = -1
                full_map_loss_to_add = -1
                count_map_loss_to_add = -1
            elif config.use_loss == 'map':
                # Average over map locations, sum over instances
                # map_loss = criterion_bce_noreduce(map, locations)
                # map_loss_to_add = map_loss.mean(axis=1).sum()
                # map_loss, map_loss_to_add = get_map_loss()
                # loss = map_loss.mean(axis=1)
                # map_loss, map_loss_to_add = get_map_loss()
                count_map_loss, all_map_loss, map_loss = get_map_loss()
                full_map_loss_to_add = all_map_loss.sum().item()
                count_map_loss_to_add = count_map_loss.sum().item()
                loss = map_loss

            elif config.use_loss == 'both':
                # map_loss = criterion_bce_noreduce(map, locations)
                # # map_loss_reduce = criterion_bce(map, locations)
                # map_loss_to_add = map_loss.mean(axis=1).sum()
                # map_loss, map_loss_to_add = get_map_loss()
                count_map_loss, all_map_loss, map_loss = get_map_loss()
                full_map_loss_to_add = all_map_loss.sum().item()
                count_map_loss_to_add = count_map_loss.sum().item()
                # loss = num_loss + map_loss.mean(axis=1)
                loss = num_loss + map_loss
            elif config.use_loss == 'map_then_both':
                # map_loss = criterion_bce_noreduce(map, locations)

                # map_loss, map_loss_to_add = get_map_loss()
                count_map_loss, all_map_loss, map_loss = get_map_loss()
                full_map_loss_to_add = all_map_loss.sum().item()
                count_map_loss_to_add = count_map_loss.sum().item()

                if ep < 150:
                    loss = map_loss
                else:
                    loss = num_loss + map_loss
            correct = pred.eq(target.view_as(pred))
            batch_results['pass count'] = pass_count.detach().cpu().numpy()
            batch_results['correct'] = correct.cpu().numpy()
            batch_results['predicted'] = pred.detach().cpu().numpy()
            batch_results['true'] = target.detach().cpu().numpy()
            batch_results['loss'] = loss.detach().cpu().numpy()
            try:
                # Somehow before it was like just the first column was going
                # into batch_results, so just map loss for the first out of
                # nine location, instead of the average over all locations.
                # batch_results['map loss'] = map_loss.mean(axis=1).detach().cpu().numpy()
                # batch_results['map loss'] = map_loss.mean(axis=1).detach().cpu().numpy()
                batch_results['full map loss'] = all_map_loss.detach().cpu().numpy()
                batch_results['count map loss'] = count_map_loss.detach().cpu().numpy()
            except:
                # batch_results['map loss'] = np.ones(loss.shape) * -1
                batch_results['full map loss'] = np.ones(loss.shape) * -1
                batch_results['count map loss'] = np.ones(loss.shape) * -1
            batch_results['num loss'] = num_loss.detach().cpu().numpy()
            batch_results['shape loss'] = shape_loss.detach().cpu().numpy()
            batch_results['epoch'] = epoch
            test_results = pd.concat((test_results, batch_results))

            n_correct += pred.eq(target.view_as(pred)).sum().item()
            epoch_loss += loss.mean().item()
            num_epoch_loss += num_loss.mean().item()
            # if not isinstance(map_loss_to_add, int):
            #     map_loss_to_add = map_loss_to_add.item()
            full_map_epoch_loss += full_map_loss_to_add
            count_map_epoch_loss += count_map_loss_to_add
            # class-specific analysis and confusion matrix
            # c = (pred.squeeze() == target)
            for j in range(target.shape[0]):
                label = target[j]
                confusion_matrix[label-config.min_num, pred[j]-config.min_num] += 1
        # These two lines should be the same
        # map_epoch_loss / len(loader.dataset)
        # test_results['map loss'].mean()

        accuracy = 100. * (n_correct/len(loader.dataset))
        epoch_loss /= len(loader)
        num_epoch_loss /= len(loader)
        # map_epoch_loss /= len(loader)
        if config.use_loss == 'num':
            # map_epoch_loss /= len(loader)
            full_map_epoch_loss /= len(loader)
            count_map_epoch_loss /= len(loader)
        else:
            full_map_epoch_loss /= len(loader.dataset)
            count_map_epoch_loss /= len(loader.dataset)
        map_epoch_loss = (full_map_epoch_loss, count_map_epoch_loss)
        shape_epoch_loss /= len(loader) * n_glimpses

        return (epoch_loss, num_epoch_loss, accuracy, shape_epoch_loss,
                map_epoch_loss, test_results, confusion_matrix)

    test_results = pd.DataFrame(columns=columns)
    for ep in range(n_epochs):
        epoch_timer = Timer()
        if nonsymbolic:
            train_f = train_nosymbol
            test_f = test_nosymbol
        else:
            train_f = train
            test_f = test
        # Train
        ep_tr_loss, ep_tr_num_loss, tr_accuracy, ep_tr_sh_loss, ep_tr_map_loss = train_f(train_loader, ep)

        train_loss[ep] = ep_tr_loss
        train_num_loss[ep] = ep_tr_num_loss
        train_acc[ep] = tr_accuracy
        train_sh_loss[ep] = ep_tr_sh_loss
        # train_map_loss[ep] = ep_tr_map_loss
        train_full_map_loss[ep], train_count_map_loss[ep] = ep_tr_map_loss
        # train_shape_loss[ep] = ep_shape_loss
        confs = [None for _ in test_loaders]
        # Test
        shape_lum = product(config.test_shapes, config.lum_sets)
        for ts, (test_loader, (test_shapes, lums)) in enumerate(zip(test_loaders, shape_lum)):
            epoch_te_loss, epoch_te_num_loss, te_accuracy, epoch_te_sh_loss, epoch_te_map_loss, epoch_df, conf = test_f(test_loader, ep)

            epoch_df['train shapes'] = str(config.train_shapes)
            epoch_df['test shapes'] = str(test_shapes)
            epoch_df['test lums'] = str(lums)
            epoch_df['repetition'] = config.rep

            test_results = pd.concat((test_results, epoch_df), ignore_index=True)
            test_loss[ts][ep] = epoch_te_loss
            test_num_loss[ts][ep] = epoch_te_num_loss
            test_acc[ts][ep] = te_accuracy
            test_sh_loss[ts][ep] = epoch_te_sh_loss
            # test_map_loss[ts][ep] = epoch_te_map_loss
            test_full_map_loss[ts][ep], test_count_map_loss[ts][ep] = epoch_te_map_loss
            confs[ts] = conf
            # base_name_test = base_name + f'_test-shapes-{test_shapes}_lums-{lums}'
            base_name_test = base_name

        if not ep % 50 or ep == n_epochs - 1 or ep==1:
            train_losses = (train_num_loss, train_full_map_loss, train_count_map_loss, train_sh_loss)
            plot_performance(test_results, train_losses, train_acc, confs, ep, config)
        epoch_timer.stop_timer()
        if isinstance(test_loss, list):
            print(f'Epoch {ep}. LR={optimizer.param_groups[0]["lr"]:.4} \t (Train/Val/TestLum/TestShape/TestBoth) Num Loss={train_num_loss[ep]:.4}/{test_num_loss[0][ep]:.4}/{test_num_loss[1][ep]:.4}/{test_num_loss[2][ep]:.4}/{test_num_loss[3][ep]:.4} \t Accuracy={train_acc[ep]:.3}%/{test_acc[0][ep]:.3}%/{test_acc[1][ep]:.3}%/{test_acc[2][ep]:.3}%/{test_acc[3][ep]:.3}% \t Shape loss: {train_sh_loss[ep]:.4}')
            print(f'Full Map loss: {train_full_map_loss[ep]:.4}/{test_full_map_loss[0][ep]:.4}/{test_full_map_loss[1][ep]:.4}/{test_full_map_loss[2][ep]:.4}/{test_full_map_loss[3][ep]:.4} \t Count Map loss: {train_count_map_loss[ep]:.4}/{test_count_map_loss[0][ep]:.4}/{test_count_map_loss[1][ep]:.4}/{test_count_map_loss[2][ep]:.4}/{test_count_map_loss[3][ep]:.4}')
        else:
            print(f'Epoch {ep}. LR={optimizer.param_groups[0]["lr"]:.4} \t (Train/Test) Num Loss={train_num_loss[ep]:.4}/{test_num_loss[ep]:.4}/ \t Accuracy={train_acc[ep]:.3}%/{test_acc[ep]:.3}% \t Shape loss: {train_sh_loss[ep]:.5} \t Map loss: {train_map_loss[ep]:.5}')

    res_tr  = [train_loss, train_acc, train_num_loss, train_sh_loss, train_full_map_loss, train_count_map_loss]
    res_te = [test_loss, test_acc, test_num_loss, test_sh_loss, test_full_map_loss, test_count_map_loss, confs, test_results]
    results_list = res_tr + res_te
    return rnn, results_list

def plot_performance(test_results, train_losses, train_acc, confs, ep, config):
    ticks = list(range(config.max_num - config.min_num + 1))
    ticklabels = [str(tick + config.min_num) for tick in ticks]
    base_name = config.base_name
    test_results['accuracy'] = test_results['correct'].astype(int)*100
    # data = test_results[test_results['test shapes'] == str(test_shapes) and test_results['test lums'] == str(lums)]
    data = test_results
    # max_pass = max(data['pass count'].max(), 6)
    # data = data[data['pass count'] < max_pass]
    make_loss_plot(data, train_losses, ep, config)
    # sns.countplot(data=test_results[test_results['correct']==True], x='epoch', hue='pass count')
    # plt.savefig(f'figures/toy/test_correct_{base_name}.png', dpi=300)
    # plt.close()

    # accuracy = data.groupby(['epoch', 'pass count']).mean()
    accuracy = data.groupby(['epoch', 'test shapes', 'test lums', 'pass count']).mean()

    plt.plot(train_acc[:ep + 1], ':', color='green', label='training accuracy')
    sns.lineplot(data=accuracy, x='epoch', hue='test shapes',
                 style='test lums', y='accuracy', alpha=0.7)
    plt.legend()
    plt.grid()
    title = f'{config.model_type} trainon-{config.train_on} train_shapes-{config.train_shapes}'
    plt.title(title)
    plt.ylim([0, 102])
    plt.ylabel('Accuracy on number task')
    plt.savefig(f'figures/toy/letters/accuracy_{base_name}.png', dpi=300)
    plt.close()

    # plt.plot(train_acc[:ep + 1], ':', color='green', label='training accuracy')
    # sns.lineplot(data=accuracy, x='epoch', hue='pass count',
    #              y='accuracy', alpha=0.7)
    # plt.legend()
    # plt.grid()
    # plt.title(title)
    # plt.ylim([0, 102])
    # plt.ylabel('Accuracy on number task')
    # plt.savefig(f'figures/toy/letters/accuracy_byintegration_{base_name_test}.png', dpi=300)
    # plt.close()

    # acc_on_difficult = accuracy.loc[ep, 5.0]['accuracy']
    # print(f'Testset {ts}, Accuracy on level 5 difficulty: {acc_on_difficult}')

    fig, axs = plt.subplots(2, 2, figsize=(10, 10))
    shape_lum = product(config.test_shapes, config.lum_sets)
    axs = axs.flatten()
    for i, (ax, (shape, lum)) in enumerate(zip(axs, shape_lum)):
        ax.matshow(confs[i])
        ax.set_aspect('equal', adjustable='box')
        ax.set_title(f'test shapes={shape} lums={lum}')
        ax.set_xticks(ticks, ticklabels)
        ax.set_xlabel('Predicted Class')
        ax.set_ylabel('True Class')
        ax.set_yticks(ticks, ticklabels)
        # ax2 = ax.twinx()
        # ax2.set_yticks(ticks, np.sum(confs[i], axis=1))
    fig.tight_layout()
    plt.savefig(f'figures/toy/letters/confusion_{base_name}.png', dpi=300)
    plt.close()


def make_loss_plot(data, train_losses, ep, config):
    train_num_loss, train_full_map_loss, _, train_sh_loss = train_losses
    ## PLOT LOSS FOR BOTH OBJECTIVES
    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=[9,9], sharex=True)
    # sns.lineplot(data=data, x='epoch', y='num loss', hue='pass count', ax=ax1)
    ax1.plot(train_num_loss[:ep + 1], ':', color='green', label='training loss')
    sns.lineplot(data=data, x='epoch', y='num loss', hue='test shapes',
                 style='test lums', ax=ax1, legend=False, alpha=0.7)

    # ax1.legend(title='Integration Difficulty')
    ax1.set_ylabel('Number Loss')
    mt = config.model_type #+ '-nosymbol' if nonsymbolic else config.model_type
    # title = f'{mt} trainon-{config.train_on} train_shapes-{config.train_shapes} \n test_shapes-{test_shapes} useloss-{config.use_loss} lums-{lums}'
    title = f'{mt} trainon-{config.train_on} train_shapes-{config.train_shapes}'
    ax1.set_title(title)
    ylim = ax1.get_ylim()
    ax1.set_ylim([-0.05, ylim[1]])
    ax1.grid()
    # plt.savefig(f'figures/toy/test_num-loss_{base_name_test}.png', dpi=300)
    # plt.close()

    # sns.lineplot(data=data, x='epoch', y='map loss', hue='pass count', ax=ax2, estimator='mean')
    ax2.plot(train_full_map_loss[:ep + 1], ':', color='green', label='training loss')
    sns.lineplot(data=data, x='epoch', y='full map loss', hue='test shapes',
                 style='test lums', ax=ax2, estimator='mean', legend=False, alpha=0.7)
    ax2.set_ylabel('Full Map Loss')
    # plt.title(title)
    ylim = ax2.get_ylim()
    ax2.set_ylim([-0.05, ylim[1]])
    ax2.grid()

    ax3.plot(train_sh_loss[:ep + 1], ':', color='green', label='training loss')
    sns.lineplot(data=data, x='epoch', y='shape loss', hue='test shapes',
                 style='test lums', ax=ax3, estimator='mean', alpha=0.7)
    ax3.set_ylabel('Shape Loss')
    # plt.title(title)
    ylim = ax3.get_ylim()
    ax3.set_ylim([-0.05, ylim[1]])
    ax3.grid()
    fig.tight_layout()
    # ax2.legend(title='Integration Difficulty')
    ax3.legend()
    plt.savefig(f'figures/toy/letters/loss_{config.base_name}.png', dpi=300)
    plt.close()


def make_loss_plot_2map(data, train_losses, ep, config):
    train_num_loss, train_full_map_loss, train_count_map_loss, train_sh_loss = train_losses
    ## PLOT LOSS FOR BOTH OBJECTIVES
    fig, (ax1, ax2, ax3, ax4) = plt.subplots(4, 1, figsize=[9,12], sharex=True)
    # sns.lineplot(data=data, x='epoch', y='num loss', hue='pass count', ax=ax1)
    ax1.plot(train_num_loss[:ep + 1], ':', color='green', label='training loss')
    sns.lineplot(data=data, x='epoch', y='num loss', hue='test shapes',
                 style='test lums', ax=ax1, legend=False, alpha=0.7)

    # ax1.legend(title='Integration Difficulty')
    ax1.set_ylabel('Number Loss')
    mt = config.model_type #+ '-nosymbol' if nonsymbolic else config.model_type
    # title = f'{mt} trainon-{config.train_on} train_shapes-{config.train_shapes} \n test_shapes-{test_shapes} useloss-{config.use_loss} lums-{lums}'
    title = f'{mt} trainon-{config.train_on} train_shapes-{config.train_shapes}'
    ax1.set_title(title)
    ylim = ax1.get_ylim()
    ax1.set_ylim([-0.05, ylim[1]])
    ax1.grid()
    # plt.savefig(f'figures/toy/test_num-loss_{base_name_test}.png', dpi=300)
    # plt.close()

    # sns.lineplot(data=data, x='epoch', y='map loss', hue='pass count', ax=ax2, estimator='mean')
    ax2.plot(train_full_map_loss[:ep + 1], ':', color='green', label='training loss')
    sns.lineplot(data=data, x='epoch', y='full map loss', hue='test shapes',
                 style='test lums', ax=ax2, estimator='mean', legend=False, alpha=0.7)
    ax2.set_ylabel('Full Map Loss')
    # plt.title(title)
    ylim = ax2.get_ylim()
    ax2.set_ylim([-0.05, ylim[1]])
    ax2.grid()

    ax3.plot(train_count_map_loss[:ep + 1], ':', color='green', label='training loss')
    sns.lineplot(data=data, x='epoch', y='count map loss', hue='test shapes',
                 style='test lums', ax=ax3, estimator='mean', legend=False, alpha=0.7)
    ax3.set_ylabel('Count Map Loss')
    # plt.title(title)
    ylim = ax3.get_ylim()
    ax3.set_ylim([-0.05, ylim[1]])
    ax3.grid()

    ax4.plot(train_sh_loss[:ep + 1], ':', color='green', label='training loss')
    sns.lineplot(data=data, x='epoch', y='shape loss', hue='test shapes',
                 style='test lums', ax=ax4, estimator='mean', alpha=0.7)
    ax4.set_ylabel('Shape Loss')
    # plt.title(title)
    ylim = ax4.get_ylim()
    ax4.set_ylim([-0.05, ylim[1]])
    ax4.grid()
    fig.tight_layout()
    # ax2.legend(title='Integration Difficulty')
    ax4.legend()
    plt.savefig(f'figures/toy/letters/loss_{config.base_name}.png', dpi=300)
    plt.close()

def save_dataset(fname, noise_level, size, pass_count_range, num_range, shapes_set, same):
    "Depreceated. Datasets should be generated in advance."
    n_shapes = 10
    data = toy.generate_dataset(noise_level, size, pass_count_range, num_range, shapes_set, n_shapes, same)
    data.to_pickle(fname)
    return data

def get_dataset(size, shapes_set, config, lums, solarize):
    """If specified dataset already exists, load it. Otherwise, create it.

    Datasets are always saved with the same time, irrespective of whether the
    dataframe contains the tetris or numeric glimpses or neither.
    """
    noise_level = config.noise_level
    min_pass_count = config.min_pass
    max_pass_count = config.max_pass
    pass_count_range = (min_pass_count, max_pass_count)
    min_num = config.min_num
    max_num = config.max_num
    num_range = (min_num, max_num)
    shape_input = config.shape_input
    same = config.same
    shapes = ''.join([str(i) for i in shapes_set])
    # solarize = config.solarize

    # fname = f'toysets/toy_dataset_num{min_num}-{max_num}_nl-{noise_level}_diff{min_pass_count}-{max_pass_count}_{shapes_set}_{size}{tet}.pkl'
    # fname_notet = f'toysets/toy_dataset_num{min_num}-{max_num}_nl-{noise_level}_diff{min_pass_count}-{max_pass_count}_{shapes_set}_{size}'
    samee = 'same' if same else ''
    if config.distract:
        challenge = '_distract'
    elif config.random:
        challenge = '_random'
    else:
        challenge = ''
    # distract = '_distract' if config.distract else ''
    solar = 'solarized_' if solarize else ''
    fname = f'toysets/toy_dataset_num{min_num}-{max_num}_nl-{noise_level}_diff{min_pass_count}-{max_pass_count}_{shapes}{samee}{challenge}_grid{config.grid}_{solar}{size}.pkl'
    fname_gw = f'toysets/toy_dataset_num{min_num}-{max_num}_nl-{noise_level}_diff{min_pass_count}-{max_pass_count}_{shapes}{samee}{challenge}_grid{config.grid}_lum{lums}_gw6_{solar}{size}.pkl'
    if os.path.exists(fname_gw):
        print(f'Loading saved dataset {fname_gw}')
        data = pd.read_pickle(fname_gw)
    # elif os.path.exists(fname):
    #     print(f'Loading saved dataset {fname}')
    #     data = pd.read_pickle(fname)
    else:
        print(f'{fname_gw} does not exist. Exiting.')
        exit()
        # print('Generating new dataset')
        # data = save_dataset(fname_gw, noise_level, size, pass_count_range, num_range, shapes_set, same)

    # Add pseudoimage glimpses if needed but not present
    if shape_input == 'tetris' and 'tetris glimpse pixels' not in data.columns:
        data = pseudo.add_tetris(fname_gw)
    elif shape_input == 'char' and 'char glimpse pixels' not in data.columns:
        data = numeric.add_chars(fname_gw)

    return data

def get_loader(dataset, train_on, cross_entropy_loss, outer, shape_format, model_type, target_type):
    """Prepare a torch DataLoader for the provided dataset.

    Other input arguments control what the input features should be and what
    datatype the target should be, depending on what loss function will be used.
    The outer argument appends the flattened outer product of the two input
    vectors (xy and shape) to the input tensor. This is hypothesized to help
    enable the network to rely on an integration of the two streams
    """
    # Create shape and or xy tensors
    if train_on == 'both' or train_on =='shape':
        dataset['shape1'] = dataset['shape']
        def convert(symbolic):
            """return array of 4 lists of nonsymbolic"""
            # dataset size x n glimpses x n shapes (100 x 4 x 9)
            # want to convert to 100 x 4 x n_shapes in this glimpse x 3)
            coords = [(x,y) for (x,y) in product([0.2, 0.5, 0.8], [0.2, 0.5, 0.8])]
            indexes = np.arange(9)
            # [word for sentence in text for  word in sentence]
            # nonsymbolic = [(glimpse[idx], coords[idx][0], coords[idx][1]) for glimpse in symbolic for idx in glimpse.nonzero()[0]]
            nonsymbolic = [[],[],[],[]]
            for i, glimpse in enumerate(symbolic):
                np.random.shuffle(indexes)
                nonsymbolic[i] = [(glimpse[idx], coords[idx][0], coords[idx][1]) for idx in indexes]
            return nonsymbolic
        if shape_format == 'parametric':
            converted = dataset['shape1'].apply(convert)
            shape_input = torch.tensor(converted).float().to(device)
            shape_label = torch.tensor(dataset['shape']).float().to(device)
            # shape = [torch.tensor(glimpse).float().to(device) for glimpse in converted]
        elif shape_format == 'tetris':
            print('Tetris pixel inputs.')
            shape_label = torch.tensor(dataset['shape']).float().to(device)
            shape_input = torch.tensor(dataset['tetris glimpse pixels']).float().to(device)
        elif shape_format == 'solarized':
            if 'cnn' in model_type:
                image_array = np.stack(dataset['solarized image'], axis=0)
                shape_input = torch.tensor(image_array).float().to(device)
                shape_input = torch.unsqueeze(shape_input, 1)  # 1 channel
            elif model_type == 'recurrent_control':
                image_array = np.stack(dataset['solarized image'], axis=0)
                nex, w, h = image_array.shape
                image_array = image_array.reshape(nex, -1)
                shape_input = torch.tensor(image_array).float().to(device)
            else:
                glimpse_array = np.stack(dataset['sol glimpse pixels'], axis=0)
                shape_input = torch.tensor(glimpse_array).float().to(device)
            shape_array = np.stack(dataset['shape'], axis=0)
            shape_label = torch.tensor(shape_array).float().to(device)
        elif shape_format == 'noise':
            if 'cnn' in model_type:
                image_array = np.stack(dataset['noised image'], axis=0)
                shape_input = torch.tensor(image_array).float().to(device)
                shape_input = torch.unsqueeze(shape_input, 1)  # 1 channel
            elif model_type == 'recurrent_control':
                image_array = np.stack(dataset['noised image'], axis=0)
                nex, w, h = image_array.shape
                image_array = image_array.reshape(nex, -1)
                shape_input = torch.tensor(image_array).float().to(device)
            else:
                glimpse_array = np.stack(dataset['noi glimpse pixels'], axis=0)
                shape_input = torch.tensor(glimpse_array).float().to(device)
            shape_array = np.stack(dataset['shape'], axis=0)
            shape_label = torch.tensor(shape_array).float().to(device)
        elif shape_format == 'pixel_std':
            glimpse_array = np.stack(dataset['char glimpse pixels'], axis=0)
            glimpse_array = np.std(glimpse_array, axis=-1) / 0.4992277987669841  # max std in training
            shape_input = torch.tensor(glimpse_array).unsqueeze(-1).float().to(device)
        elif shape_format == 'pixel_count':
            glimpse_array = np.stack(dataset['char glimpse pixels'], axis=0)
            n, s, _ = glimpse_array.shape
            all_counts = np.zeros((n, s, 1))
            for i, seq in enumerate(glimpse_array):
                for j, glimpse in enumerate(seq):
                    unique, counts = np.unique(glimpse, return_counts=True)
                    all_counts[i, j, 0] = counts.min()/36
            # unique, counts = np.unique(glimpse_array[0], return_counts=True, axis=0)
            shape_input = torch.tensor(all_counts).float().to(device)
        elif shape_format == 'symbolic': # symbolic shape input
            shape_array = np.stack(dataset['shape'], axis=0)
            # shape_input = torch.tensor(dataset['shape']).float().to(device)
            shape_input = torch.tensor(shape_array).float().to(device)
            shape_label = torch.tensor(shape_array).float().to(device)
    if train_on == 'both' or train_on == 'xy':
        xy_array = np.stack(dataset['xy'], axis=0)
        # xy_array = np.stack(dataset['glimpse coords'], axis=0)
        # norm_xy_array = xy_array/20
        # xy should now already be the original scaled xy between 0 and 1. No need to rescale (since alphabetic)
        norm_xy_array = xy_array * 1.2
        # norm_xy_array = xy_array / 21
        # xy = torch.tensor(dataset['xy']).float().to(device)
        xy = torch.tensor(norm_xy_array).float().to(device)

    # Create merged input (or not)
    if train_on == 'xy':
        input = xy
    elif train_on == 'shape':
        input = shape_input
    elif train_on == 'both':
        if outer:
            assert shape_format != 'parametric'  # not implemented outer with nonsymbolic
            # dataset['shape.t'] = dataset['shape'].apply(lambda x: np.transpose(x))
            # kernel = np.outer(sh, xy) for sh, xy in zip
            def get_outer(xy, shape):
                return [np.outer(x,s).flatten() for x, s in zip(xy, shape)]
            dataset['kernel'] = dataset.apply(lambda x: get_outer(x.xy, x.shape1), axis=1)
            kernel = torch.tensor(dataset['kernel']).float().to(device)
            input = torch.cat((xy, shape_input, kernel), dim=-1)
        else:
            input = torch.cat((xy, shape_input), dim=-1)

    if cross_entropy_loss:
        if target_type == 'all':
            total_num = dataset['locations'].apply(sum)
            target = torch.tensor(total_num).long().to(device)
        else:
            target = torch.tensor(dataset['numerosity']).long().to(device)
    else:
        target = torch.tensor(dataset['numerosity']).float().to(device)
    pass_count = torch.tensor(dataset['pass count']).float().to(device)
    # true_loc = torch.tensor(dataset['locations']).float().to(device)
    if 'locations_to_count' in dataset.columns:
        count_loc = torch.tensor(dataset['locations_to_count']).float().to(device)
        all_loc = torch.tensor(dataset['locations']).float().to(device)
        true_loc = (all_loc, count_loc)
    else:
        true_loc = torch.tensor(dataset['locations']).float().to(device)

    if shape_format == 'parametric':
        dset = TensorDataset(xy, shape_input, target, all_loc, shape_label, pass_count)
    if 'map_gated' in model_type:
        dset = TensorDataset(input, target, all_loc, count_loc, shape_label, pass_count)
    else:
        dset = TensorDataset(input, target, all_loc, shape_label, pass_count)
        # dset = TensorDataset(input, target, true_loc, None, shape_label, pass_count)
    loader = DataLoader(dset, batch_size=BATCH_SIZE, shuffle=True)
    return loader

def get_model(model_type, train_on, **mod_args):
    hidden_size = mod_args['h_size']
    output_size = mod_args['n_classes'] + 1
    shape_format = mod_args['format']
    grid = mod_args['grid']
    n_shapes = mod_args['n_shapes']
    grid_to_im_shape = {3:[27, 24], 6:[48, 42], 9:[69, 60]}
    height, width = grid_to_im_shape[grid]
    map_size = grid**2
    xy_sz = 2
    if shape_format == 'tetris':
        sh_sz = 4*4
    elif shape_format == 'symbolic':
        # sh_sz = 64 # 8 * 8
        # sh_sz = 9
        sh_sz = n_shapes#20#25
    elif 'pixel' in shape_format:
        sh_sz = 1
    else:
        sh_sz = 6 * 6
    in_sz = xy_sz if train_on=='xy' else sh_sz if train_on =='shape' else sh_sz + xy_sz
    if train_on == 'both' and mod_args['outer']:
        in_sz += xy_sz * sh_sz
    if 'num_as_mapsum' in model_type:
        if shape_format == 'parametric':  #no_symbol:
            model = mod.NumAsMapsum_nosymbol(in_sz, hidden_size, map_size, output_size, **mod_args).to(device)
        elif '2stream' in model_type:
            model = mod.NumAsMapsum2stream(sh_sz, hidden_size, map_size, output_size, **mod_args).to(device)
        else:
            model = mod.NumAsMapsum(in_sz, hidden_size, output_size, **mod_args).to(device)
    elif 'gated' in model_type:
        if 'map' in model_type:
            if '2' in model_type:
                model = mod.MapGated2RNN(sh_sz, hidden_size, map_size, output_size, **mod_args).to(device)
            else:
                model = mod.MapGatedSymbolicRNN(sh_sz, hidden_size, map_size, output_size, **mod_args).to(device)
        else:
            model = mod.GatedSymbolicRNN(sh_sz, hidden_size, map_size, output_size, **mod_args).to(device)
    elif 'rnn_classifier' in model_type:
        if model_type == 'rnn_classifier_par':
            # Model with two parallel streams at the level of the map. Only one
            # stream is optimized to match the map. The other of the same size
            # is free, only influenced by the num loss.
            mod_args['parallel'] = True
        elif '2stream' in model_type:
            model = mod.RNNClassifier2stream(sh_sz, hidden_size, map_size, output_size, **mod_args).to(device)
        elif shape_format == 'parametric':  #no_symbol:
            model = mod.RNNClassifier_nosymbol(in_sz, hidden_size, output_size, **mod_args).to(device)
        else:
            model = mod.RNNClassifier(in_sz, hidden_size, map_size, output_size, **mod_args).to(device)
    elif model_type == 'recurrent_control':
        # in_sz = 21 * 27  # Size of the images
        in_size = height * width
        model = mod.RNNClassifier(in_sz, hidden_size, output_size, **mod_args).to(device)
    elif model_type == 'rnn_regression':
        model = mod.RNNRegression(in_sz, hidden_size, map_size, output_size, **mod_args).to(device)
    elif model_type == 'mult':
        model = mod.MultiplicativeModel(in_sz, hidden_size, output_size, **mod_args).to(device)
    elif model_type == 'hyper':
        model = mod.HyperModel(in_sz, hidden_size, output_size).to(device)
    elif 'cnn' in model_type:
        # width = 24 #21
        # height = 27
        dropout = mod_args['dropout']
        if model_type == 'bigcnn':
            mod_args['big'] = True
            model = ConvNet(width, height, map_size, output_size, **mod_args).to(device)
        else:
            model = ConvNet(width, height, map_size, output_size, **mod_args).to(device)
    else:
        print(f'Model type {model_type} not implemented. Exiting')
        exit()
    # if small_weights:
    #     model.init_small()  # not implemented yet
    print('Params to learn:')
    for name, param in model.named_parameters():
        if param.requires_grad:
            print(f"\t {name} {param.shape}")
    total_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print(f'Total number of trainable model params: {total_params}')



    def count_parameters(model):
        table = PrettyTable(["Modules", "Parameters"])
        total_params = 0
        for name, parameter in model.named_parameters():
            if not parameter.requires_grad: continue
            params = parameter.numel()
            table.add_row([name, params])
            total_params+=params
        print(table)
        print(f"Total Trainable Params: {total_params}")
        return total_params

    count_parameters(model)
    return model

def get_config():
    parser = argparse.ArgumentParser(description='PyTorch network settings')
    parser.add_argument('--model_type', type=str, default='num_as_mapsum', help='rnn_classifier rnn_regression num_as_mapsum cnn')
    parser.add_argument('--target_type', type=str, default='all', help='all or notA')
    parser.add_argument('--train_on', type=str, default='xy', help='xy, shape, or both')
    parser.add_argument('--noise_level', type=float, default=1.6)
    parser.add_argument('--train_size', type=int, default=100000)
    parser.add_argument('--test_size', type=int, default=5000)
    parser.add_argument('--grid', type=int, default=9)
    parser.add_argument('--n_iters', type=int, default=1, help='how many times the rnn should loop through sequence')
    parser.add_argument('--rotate', action='store_true', default=False)  # not implemented
    parser.add_argument('--small_weights', action='store_true', default=False)  # not implemented
    parser.add_argument('--n_epochs', type=int, default=500)
    parser.add_argument('--use_loss', type=str, default='both', help='num, map or both')
    parser.add_argument('--pretrained', type=str, default='num_as_mapsum-xy_nl-0.0_niters-1_5eps_10000_map-loss_ep-5.pt')  # not implemented
    parser.add_argument('--outer', action='store_true', default=False)
    parser.add_argument('--h_size', type=int, default=25)
    parser.add_argument('--min_pass', type=int, default=0)
    parser.add_argument('--max_pass', type=int, default=6)
    parser.add_argument('--min_num', type=int, default=2)
    parser.add_argument('--max_num', type=int, default=7)
    parser.add_argument('--act', type=str, default=None)
    parser.add_argument('--alt_rnn', action='store_true', default=False)
    # parser.add_argument('--no_symbol', action='store_true', default=False)
    parser.add_argument('--train_shapes', type=list, default=[0, 1, 2, 3, 5, 6, 7, 8], help='Can either be a string of numerals 0123 or letters ABCD.')
    parser.add_argument('--test_shapes', nargs='*', type=list, default=[[0, 1, 2, 3, 5, 6, 7, 8], [4]])
    parser.add_argument('--detach', action='store_true', default=False)
    parser.add_argument('--learn_shape', action='store_true', default=False, help='for the parametric shape rep, whether to additional train to produce symbolic shape labels')
    parser.add_argument('--shape_input', type=str, default='symbolic', help='Which format to use for what pathway (symbolic, parametric, tetris, or char)')
    parser.add_argument('--same', action='store_true', default=False)
    parser.add_argument('--distract', action='store_true', default=False)
    parser.add_argument('--random', action='store_true', default=False)
    parser.add_argument('--solarize', action='store_true', default=False)
    parser.add_argument('--rep', type=int, default=0)
    # parser.add_argument('--tetris', action='store_true', default=False)
    # parser.add_argument('--no_cuda', action='store_true', default=False)
    # parser.add_argument('--preglimpsed', type=str, default=None)
    # parser.add_argument('--use_schedule', action='store_true', default=False)
    parser.add_argument('--dropout', type=float, default=0.0)
    # parser.add_argument('--drop_rnn', type=float, default=0.1)
    # parser.add_argument('--wd', type=float, default=0) # 1e-6
    # parser.add_argument('--lr', type=float, default=0.01)
    # parser.add_argument('--debug', action='store_true', default=False)
    # parser.add_argument('--bce', action='store_true', default=False)
    config = parser.parse_args()
    # Convert string input argument into a list of indices
    if config.train_shapes[0].isnumeric():
        config.train_shapes = [int(i) for i in config.train_shapes]
        for j, test_set in enumerate(config.test_shapes):
            config.test_shapes[j] = [int(i) for i in test_set]
    elif config.train_shapes[0].isalpha():
        letter_map = {'A':0, 'B':1, 'C':2, 'D':3, 'E':4, 'F':5, 'G':6, 'H':7,
                      'J':8, 'K':9, 'N':10, 'O':11, 'P':12, 'R':13, 'S':14,
                      'U':15, 'Z':16}
        config.shapestr = config.train_shapes.copy()
        config.testshapestr = config.test_shapes.copy()
        config.train_shapes = [letter_map[i] for i in config.train_shapes]
        for j, test_set in enumerate(config.test_shapes):
            config.test_shapes[j] = [letter_map[i] for i in test_set]
    print(config)
    return config

def main():
    # Process input arguments
    config = get_config()
    model_type = config.model_type
    target_type = config.target_type
    # if model_type == 'num_as_mapsum' or model_type == 'rnn_regression':
    if model_type == 'rnn_regression':
        config.cross_entropy = False
    else:
        config.cross_entropy = True

    train_on = config.train_on
    if model_type == 'recurrent_control' and train_on != 'shape':
        print('Recurrent control requires --train_on=shape (pixel inputs only)')
        exit()
    noise_level = config.noise_level
    train_size = config.train_size
    test_size = config.test_size
    n_iters = config.n_iters
    n_epochs = config.n_epochs
    min_pass = config.min_pass
    max_pass = config.max_pass
    pass_range = (min_pass, max_pass)
    min_num = config.min_num
    max_num = config.max_num
    num_range = (min_num, max_num)
    use_loss = config.use_loss
    drop = config.dropout

    # Prepare base file name for results files
    kernel = '-kernel' if config.outer else ''
    act = '-' + config.act if config.act is not None else ''
    alt_rnn = '2'
    detach = '-detach' if config.detach else ''
    model_desc = f'{model_type}{alt_rnn}{detach}{act}_hsize-{config.h_size}_input-{train_on}{kernel}_{config.shape_input}'
    same = 'same' if config.same else ''
    if config.distract:
        challenge = '_distract'
    elif config.random:
        challenge = '_random'
    else:
        challenge = ''
    # distract = '_distract' if config.distract else ''
    solar = 'solarized_' if config.solarize else ''
    shapes = ''.join([str(i) for i in config.shapestr])
    data_desc = f'num{min_num}-{max_num}_nl-{noise_level}_diff-{min_pass}-{max_pass}_grid{config.grid}_trainshapes-{shapes}{same}{challenge}_gw6_{solar}{train_size}'
    # train_desc = f'loss-{use_loss}_niters-{n_iters}_{n_epochs}eps'
    withshape = '+shape' if config.learn_shape else ''
    train_desc = f'loss-{use_loss}{withshape}_drop{drop}_count-{target_type}_{n_epochs}eps_rep{config.rep}'
    base_name = f'{model_desc}_{data_desc}_{train_desc}'
    if config.small_weights:
        base_name += '_small'
    config.base_name = base_name

    # make sure all results directories exist
    model_dir = 'models/toy/letters'
    results_dir = 'results/toy/letters'
    fig_dir = 'figures/toy/letters'
    dir_list = [model_dir, results_dir, fig_dir]
    for directory in dir_list:
        if not os.path.exists(directory):
            os.makedirs(directory)

    # Prepare datasets and torch dataloaders
    # config.lum_sets = [[0.0, 0.5, 1.0], [0.1, 0.3, 0.7, 0.9]]
    config.lum_sets = [[0.1, 0.5, 0.9], [0.2, 0.4, 0.6, 0.8]]
    # trainset = get_dataset(train_size, config.shapestr, config, [0.0, 0.5, 1.0], solarize=config.solarize)
    trainset = get_dataset(train_size, config.shapestr, config, [0.1, 0.5, 0.9], solarize=config.solarize)
    testsets = [get_dataset(test_size, test_shapes, config, lums, solarize=config.solarize) for test_shapes, lums in product(config.testshapestr, config.lum_sets)]
    train_loader = get_loader(trainset, config.train_on, config.cross_entropy, config.outer, config.shape_input, model_type, target_type)
    test_loaders = [get_loader(testset, config.train_on, config.cross_entropy, config.outer, config.shape_input, model_type, target_type) for testset in testsets]
    loaders = [train_loader, test_loaders]
    if target_type == 'all':
        max_num += 2
        config.max_num += 2

    # Prepare model and optimizer
    no_symbol = True if config.shape_input == 'parametric' else False
    n_classes = max_num
    n_shapes = 25 # 20
    mod_args = {'h_size': config.h_size, 'act': config.act,
                'small_weights': config.small_weights, 'outer':config.outer,
                'detach': config.detach, 'format':config.shape_input,
                'n_classes':n_classes, 'dropout': drop, 'grid': config.grid,
                'n_shapes':n_shapes}
    model = get_model(model_type, train_on, **mod_args)
    opt = SGD(model.parameters(), lr=start_lr, momentum=mom, weight_decay=wd)
    # scheduler = StepLR(opt, step_size=n_epochs/10, gamma=0.7)
    scheduler = StepLR(opt, step_size=n_epochs/20, gamma=0.7)


    # Train model and save trained model
    model, results = train_model(model, opt, scheduler, loaders, config)
    print('Saving trained model and results files...')
    torch.save(model.state_dict(), f'{model_dir}/toy_model_{base_name}_ep-{n_epochs}.pt')

    # Organize and save results
    train_loss, train_acc, train_num_loss, train_shape_loss, train_full_map_loss, train_count_map_loss, test_loss, test_acc, test_num_loss, test_shape_loss, test_full_map_loss, test_count_map_loss, conf, test_results = results
    test_results.to_pickle(f'{results_dir}/detailed_test_results_{base_name}.pkl')
    df_train = pd.DataFrame()
    df_test_list = [pd.DataFrame() for _ in range(len(test_loss))]
    df_train['loss'] = train_loss
    # df_train['map loss'] = train_map_loss
    df_train['full map loss'] = train_full_map_loss
    df_train['count map loss'] = train_count_map_loss
    df_train['num loss'] = train_num_loss
    df_train['shape loss'] = train_shape_loss
    df_train['accuracy'] = train_acc
    df_train['epoch'] = np.arange(n_epochs)
    df_train['rnn iterations'] = n_iters
    df_train['dataset'] = 'train'
    for ts, (test_shapes, test_lums) in enumerate(product(config.test_shapes, config.lum_sets)):
        df_test_list[ts]['loss'] = test_loss[ts]
        df_test_list[ts]['num loss'] = test_num_loss[ts]
        df_test_list[ts]['shape loss'] = test_shape_loss[ts]
        # df_test_list[ts]['map loss'] = test_map_loss[ts]
        df_test_list[ts]['full map loss'] = test_full_map_loss[ts]
        df_test_list[ts]['count map loss'] = test_count_map_loss[ts]
        df_test_list[ts]['accuracy'] = test_acc[ts]
        df_test_list[ts]['dataset'] = f'test {test_shapes} {test_lums}'
        df_test_list[ts]['test shapes'] = str(test_shapes)
        df_test_list[ts]['test lums'] = str(test_lums)
        df_test_list[ts]['epoch'] = np.arange(n_epochs)

    np.save(f'{results_dir}/confusion_{base_name}', conf)

    df_test = pd.concat(df_test_list)
    df_test['rnn iterations'] = n_iters
    df = pd.concat((df_train, df_test))
    df.to_pickle(f'{results_dir}/toy_results_{base_name}.pkl')


if __name__ == '__main__':
    main()


# Eventually the plot we want to make is
# sns.countplot(data=correct, x='pass count', hue='rnn iterations')
