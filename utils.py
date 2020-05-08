import torch
import torch.distributed as dist
import torch.cuda.comm as comm


def get_class_split(num_classes, num_gpus):
    class_split = []
    for i in range(num_gpus):
        _class_num = num_classes // num_gpus
        if i < (num_classes % num_gpus):
            _class_num += 1
        class_split.append(_class_num)
    return class_split


def get_onehot_label(labels, num_gpus, num_classes, model_parallel=False, class_split=None):
    # Get one-hot labels
    labels = labels.view(-1, 1)
    labels_onehot = torch.zeros(len(labels), num_classes).cuda()
    labels_onehot.scatter_(1, labels, 1)

    if not model_parallel:
        return labels_onehot
    else:
        label_tuple = comm.scatter(labels_onehot, range(num_gpus), class_split, dim=1)
        return label_tuple


def get_sparse_onehot_label_dist(opt, labels, class_split):
    labels_gather = [torch.zeros_like(labels) for _ in range(opt.world_size)]
    dist.all_gather(labels_gather, labels)
    labels = torch.cat(labels_gather)
    labels_list = labels.tolist()
    batch_size = len(labels_list)

    assert opt.world_size == len(class_split), "number of class splits NOT equals to number of gpus!"
    # prepare dict for generating sparse tensor
    splits_dict = {}
    start_index = 0
    for i, num_splits in enumerate(class_split):
        end_index = start_index + num_splits
        splits_dict[i] = {
                            "start_index": start_index,
                            "end_index": end_index,
                            "num_splits": num_splits,
                            "index_list": [],
                            "nums": 0
                        }
        start_index = end_index
    # get valid index in each split
    for i, label in enumerate(labels_list):
        for j in range(opt.world_size):
            if label >= splits_dict[j]["start_index"] and label < splits_dict[j]["end_index"]:
                valid_index = [i, label - splits_dict[j]["start_index"]]
                splits_dict[j]["index_list"].append(valid_index)
                splits_dict[j]["nums"] += 1
                break
    # finally get the sparse tensor
    label_tuple = []
    for i in range(opt.world_size):
        if splits_dict[i]["nums"] == 0:
            sparse_tensor = torch.sparse.LongTensor(torch.Size([batch_size, splits_dict[i]["num_splits"]]))
            label_tuple.append(sparse_tensor.to(i))
        else:
            sparse_index = torch.LongTensor(splits_dict[i]["index_list"])
            sparse_value = torch.ones(splits_dict[i]["nums"], dtype=torch.long)
            sparse_tensor = torch.sparse.LongTensor(
                                                sparse_index.t(),
                                                sparse_value,
                                                torch.Size([batch_size, splits_dict[i]["num_splits"]])
                                            )
            label_tuple.append(sparse_tensor.to(i))
    return labels, tuple(label_tuple)


def compute_batch_acc(outputs, labels, batch_size, model_parallel, step):
    '''compute the batch accuracy accroding to the predictions and groud-truth labels
    the complex case here is when `model_parallel` is True, the predictions logits is
    located in different gpus, if we don't want to concat them to increase gpu memory,
    we need to collect max value of it one by one

    Args:
        outputs (torch.Tensor or list of torch.Tensor): if `model_parallel` is false,
            the `outputs` is a single torch.Tensor, if `model_parallel` is True, outputs
            is a tuple of torch.Tensor which located on different gpus
        labels (torch.Tensor): generated by pytorch dataloader
        batch_size (int): batch size
        model_parallel (bool): model parallel flag
        step (int): training step in each iteration

    Returns:
        accuracy (float)
    '''
    if model_parallel:
        if not (step > 0 and step % 10 == 0):
            return 0
        outputs = [outputs]
        max_score = None
        max_preds = None
        base = 0
        for logit_same_tuple in zip(*outputs):
            _split = logit_same_tuple[0].size()[1]
            score, preds = torch.max(sum(logit_same_tuple).data, dim=1)
            score = score.to(0)
            preds = preds.to(0)
            if max_score is not None:
                cond = score > max_score
                max_preds = torch.where(cond, preds + base, max_preds)
                max_score = torch.where(cond, score, max_score)
            else:
                max_score = score
                max_preds = preds
            base += _split
        preds = max_preds
        batch_acc = torch.sum(preds == labels).item() / batch_size
    else:
        _, preds = torch.max(outputs.data, 1)
        batch_acc = torch.sum(preds == labels).item() / batch_size

    return batch_acc


def compute_batch_acc_dist(opt, outputs, labels, batch_size, class_split):
    # NOTE: labels here are total labels
    assert opt.world_size == len(class_split), "world size should equal to the number of class split"
    base = sum(class_split[:opt.rank])

    scores, preds = torch.max(outputs.data, dim=1)
    preds += base

    batch_size = labels.size(0)

    # all_gather
    scores_gather = [torch.zeros_like(scores) for _ in range(opt.world_size)]
    dist.all_gather(scores_gather, scores)
    preds_gather = [torch.zeros_like(preds) for _ in range(opt.world_size)]
    dist.all_gather(preds_gather, preds)
    # stack
    _scores = torch.stack(scores_gather)
    _preds = torch.stack(preds_gather)
    _, idx = torch.max(_scores, dim=0)
    idx = torch.stack([idx, torch.range(0, batch_size - 1).long().cuda()])
    preds = _preds[tuple(idx)]

    batch_acc = torch.sum(preds == labels).item() / batch_size

    return batch_acc


def see_memory_usage(opt, debug_indicator):
    if opt.local_rank == 0:
        print("="*20, debug_indicator, "="*20)
        print("Memory Allocated ",
              torch.cuda.memory_allocated() / (1024 * 1024 * 1024),
              "GigaBytes")
        print("Max Memory Allocated ",
              torch.cuda.max_memory_allocated() / (1024 * 1024 * 1024),
              "GigaBytes")
        print("Cache Allocated ",
              torch.cuda.memory_cached() / (1024 * 1024 * 1024),
              "GigaBytes")
        print("Max cache Allocated ",
              torch.cuda.max_memory_cached() / (1024 * 1024 * 1024),
              "GigaBytes")
        print("="*50)

if __name__ == "__main__":
    # import os
    # os.environ["CUDA_VISIBLE_DEVICES"] = "1,2,3,4"
    # labels = torch.tensor([5, 2, 3, 4, 6, 9, 7, 1]).cuda()
    # label_tuple = get_onehot_label(labels, 4, 12, [3, 3, 3, 3])
    # for label in label_tuple:
    #     print(label.size())
    #     print(label)
    labels = torch.tensor([5, 2, 3, 4, 6, 9, 7, 1])
    print(labels)
    label_tuple = get_sparse_onehot_label(labels, 4, 12, True, [3, 3, 3, 3])
    for label in label_tuple:
        print(label.size())
        print(label.to_dense())
