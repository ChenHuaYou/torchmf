import collections

import numpy as np
import torch
import torch.autograd
from torch.autograd import Variable
from torch import nn
import torch.utils.data as data
from tqdm import tqdm


class Interactions(data.Dataset):
    """
    Hold data in the form of an interactions matrix.
    Typical use-case is like a ratings matrix:
    - Users are the rows
    - Items are the columns
    - Elements of the matrix are the ratings given by a user for an item.
    """

    def __init__(self, train_data, test_data=None, train=True):
        self.train = train
        self.train_data = train_data.tocoo()
        self.test_data = test_data.tocoo()
        self.n_users = self.train_data.shape[0]
        self.n_items = self.train_data.shape[1]

        self.train_row = torch.from_numpy(self.train_data.row.astype(np.long))
        self.train_col = torch.from_numpy(self.train_data.col.astype(np.long))
        self.train_val = torch.from_numpy(self.train_data.data.astype(np.float32))

        self.test_row = torch.from_numpy(self.test_data.row.astype(np.long))
        self.test_col = torch.from_numpy(self.test_data.col.astype(np.long))
        self.test_val = torch.from_numpy(self.test_data.data.astype(np.float32))

    def __getitem__(self, index):
        if self.train:
            row = self.train_row[index]
            col = self.train_col[index]
            val = self.train_val[index]
        else:
            row = self.test_row[index]
            col = self.test_col[index]
            val = self.test_val[index]

        return (row, col), val

    def __len__(self):
        if self.train:
            return self.train_data.nnz
        else:
            return self.test_data.nnz


class BaseModule(nn.Module):
    """
    Base module for explicit matrix factorization.
    """
    
    def __init__(self,
                 n_users,
                 n_items,
                 n_factors=40,
                 dropout_p=0,
                 loss_function=nn.MSELoss(size_average=False)):
        """

        Parameters
        ----------
        n_users : int
            Number of users
        n_items : int
            Number of items
        n_factors : int
            Number of latent factors (or embeddings or whatever you want to
            call it).
        dropout_p : float
            p in nn.Dropout module. Probability of dropout.
        loss_function
            Torch loss function. Not technically needed here, but it's nice
            to attach for later usage.
        """
        super(BaseModule, self).__init__()
        self.n_users = n_users
        self.n_items = n_items
        # We have to do everything as doubles because of this silly dataloader
        # stuff https://github.com/pytorch/pytorch/blob/master/torch/utils/data/dataloader.py#L72
        # Primary issue is that torch.from_numpy() seems to cast things from
        # float32 to Double, I think? Maybe I need a custom collate function?
        self.user_biases = nn.Embedding(n_users, 1).double()
        self.item_biases = nn.Embedding(n_items, 1).double()
        self.user_embeddings = nn.Embedding(n_users, n_factors).double()
        self.item_embeddings = nn.Embedding(n_items, n_factors).double()
        
        self.dropout_p = dropout_p
        self.dropout = nn.Dropout(p=self.dropout_p)

        self.loss_function = loss_function
        
    def forward(self, users, items):
        """
        Forward pass through the model. For a single user and item, this
        looks like:

        user_bias + item_bias + user_embeddings.dot(item_embeddings)

        Parameters
        ----------
        users : np.ndarray
            Array of user indices
        items : np.ndarray
            Array of item indices

        Returns
        -------
        preds : np.ndarray
            Predicted ratings.

        """
        ues = self.user_embeddings(users)
        uis = self.item_embeddings(items)

        preds = self.user_biases(users) + self.item_biases(items)
        preds += (self.dropout(ues) * self.dropout(uis)).sum(1)

        return preds
    
    def __call__(self, *args):
        return self.forward(*args)


class BPRModule(BaseModule):
    
    def __init__(self,
                 n_users,
                 n_items,
                 n_factors=40,
                 dropout_p=0):
        super(BPRModule, self).__init__(
            n_users,
            n_items,
            n_factors=n_factors,
            dropout_p=dropout_p
        )
        
    def forward(self, users, pos_items, neg_items):
        ues = self.user_embeddings(users)
        uis = self.item_embeddings(pos_items) - self.item_embeddings(neg_items)
        preds = (self.dropout(ues) * self.dropout(uis)).sum(1)

        preds += self.user_biases(users)
        preds += self.item_biases(pos_items) - self.item_biases(neg_items)
        return preds


class BasePipeline:
    """
    Class defining a training pipeline. Instantiates data loaders, model,
    and optimizer. Handles training for multiple epochs and keeping track of
    train and test loss.
    """

    def __init__(self,
                 train_data,
                 test_data=None,
                 model=BaseModule,
                 n_factors=40,
                 batch_size=32,
                 dropout_p=0.02,
                 lr=0.01,
                 weight_decay=0.,
                 optimizer=torch.optim.Adam,
                 loss_function=nn.MSELoss(size_average=False),
                 n_epochs=10,
                 verbose=False,
                 random_seed=None):
        self.train_interactions = Interactions(train_data,
                                               test_data=test_data,
                                               train=True)
        self.train_loader = data.DataLoader(
            self.train_interactions, batch_size=1024, shuffle=True,
        )
        if test_data is not None:
            self.test_interactions = Interactions(train_data,
                                                  test_data=test_data,
                                                  train=False)
            self.test_loader = data.DataLoader(
                self.test_interactions, batch_size=1024, shuffle=True,
            )

        self.n_users = train_data.shape[0]
        self.n_items = train_data.shape[1]
        self.n_factors = n_factors
        self.batch_size = batch_size
        self.dropout_p = dropout_p
        self.lr = lr
        self.weight_decay = weight_decay
        self.loss_function = loss_function
        self.n_epochs = n_epochs
        self.model = model(self.n_users,
                           self.n_items,
                           n_factors=self.n_factors,
                           dropout_p=self.dropout_p,
                           loss_function=self.loss_function)
        self.optimizer = optimizer(self.model.parameters(),
                                   lr=self.lr,
                                   weight_decay=self.weight_decay)
        self.warm_start = False
        self.losses = collections.defaultdict(list)
        self.verbose = verbose
        if random_seed is not None:
            torch.manual_seed(random_seed)
            np.random.seed(random_seed)

    def fit(self):
        for epoch in range(self.n_epochs):
            self.losses['train'].append(self._fit_epoch())
            row = 'Epoch: {0:^3} | {1:^10.5f} | '.format(epoch, self.losses['train'][-1])
            if self.test_interactions is not None:
                self.losses['test'].append(self._validation_loss())
                row += '{0:^10.5f}'.format(self.losses['test'][-1])
            self.losses['epoch'].append(epoch)
            if self.verbose:
                print(row)

    def _fit_epoch(self):
        self.model.train()
        total_loss = torch.Tensor([0])
        for batch_idx, ((row, col), val) in tqdm(enumerate(self.train_loader)):
            row = Variable(row)
            col = Variable(col)
            val = Variable(val)
            self.optimizer.zero_grad()
            preds = self.model(row, col)
            loss = self.model.loss_function(preds, val)
            loss.backward()
            self.optimizer.step()
            total_loss += loss.data[0]
        total_loss /= len(self.train_interactions)
        return total_loss[0]

    def _validation_loss(self):
        self.model.eval()
        total_loss = torch.Tensor([0])
        for batch_idx, ((row, col), val) in enumerate(self.test_loader):
            row = Variable(row)
            col = Variable(col)
            val = Variable(val)
            preds = self.model(row, col)
            loss = self.model.loss_function(preds, val)
            total_loss += loss.data[0]
        total_loss /= len(self.test_interactions)
        return total_loss[0]
