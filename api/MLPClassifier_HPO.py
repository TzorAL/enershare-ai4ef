import numpy as np
import pandas as pd
from matplotlib import pyplot as plt

import os
import gc
import tempfile
import warnings
import copy
import tempfile
import pickle

import optuna
from optuna.integration import PyTorchLightningPruningCallback

from sklearn.preprocessing import MinMaxScaler
from sklearn.preprocessing import LabelEncoder
from sklearn.model_selection import train_test_split

from scipy.stats import zscore


import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import TensorDataset, DataLoader
from torchmetrics.classification import MultilabelAccuracy, MultilabelPrecision, MultilabelRecall, MultilabelF1Score, MultilabelHammingDistance

import pytorch_lightning as pl
from pytorch_lightning import Trainer
from pytorch_lightning.callbacks import EarlyStopping
import logging
import click

#### Define the model and hyperparameters
class Classifier(pl.LightningModule):
    """
    Classifier  Techniques are used when the output is real-valued based on continuous variables. 
                For example, any time series data. This technique involves fitting a line
    Feature: Features are individual independent variables that act as the input in your system. 
             Prediction models use features to make predictions. 
             New features can also be obtained from old features using a method known as ‘feature engineering’. 
             More simply, you can consider one column of your data set to be one feature. 
             Sometimes these are also called attributes. T
             The number of features are called dimensions
    Target: The target is whatever the output of the input variables. 
            In our case, it is the output value range of load. 
            If the training set is considered then the target is the training output values that will be considered.
    Labels: Label: Labels are the final output. You can also consider the output classes to be the labels. 
            When data scientists speak of labeled data, they mean groups of samples that have been tagged to one or more labels.

    ### The Model ### 
    Initialize the layers
    Here we have:
        one input layer (size 'lookback_window'), 
        one output layer (size 36 as we are predicting next 36 hours)
        hidden layers define by 'params' argument of init
    """
    def __init__(self, **params):
        super(Classifier, self).__init__()

        # enable Lightning to store all the provided arguments 
        # under the self.hparams attribute. 
        # These hyperparameters will also be stored within the model checkpoint
        self.save_hyperparameters()
        
        # used by trainer logger (check log_graph flag)
        # example of input use by model (random tensor of same size)
        self.example_input_array = torch.rand(self.hparams.input_dim)

        # self.loss = MeanAbsolutePercentageError() #MAPE
        self.loss = nn.BCELoss()

        """
        feature_extractor: all layers before classifier
        classifier: last layer connecting output with rest of network (not always directly)
        We load proper pretrained model, and use its feauture_extractor for the new untrained one
        (Also check forward pass commentary)
        """
        self.feature_extractor = None        
        self.classifier = None

        feature_layers, last_dim = self.make_hidden_layers()
        self.feature_extractor = nn.Sequential(*feature_layers) #list of nn layers
        self.classifier = nn.Linear(last_dim, 4)

    def make_hidden_layers(self):
        """
        Each loop is the setup of a new layer
        At each iteration:
            1. add previous layer to the next (with parameters gotten from layer_sizes)
                    at first iteration previous layer is input layer
            2. add activation function
            3. set current_layer as next layer
        connect last layer with cur_layer
        Parameters: None
        Returns: 
            layers: list containing input layer through last hidden one
            cur_layer: size (dimension) of the last hidden layer      
        """
        layers = [] # list of layer to add at NN
        cur_layer = self.hparams.input_dim

        # layer_sizes must be iterable for creating model layers
        if(not isinstance(self.hparams.layer_sizes,list)):
            self.hparams.layer_sizes = [self.hparams.layer_sizes]         
        for next_layer in self.hparams.layer_sizes: 
            layers.append(nn.Linear(int(cur_layer), int(next_layer)))
            layers.append(getattr(nn, self.hparams.activation)()) # nn.activation_function (as suggested by Optuna)
            cur_layer = int(next_layer) #connect cur_layer with previous layer (at first iter, input layer)
            # print(f'({self.hparams.layer_sizes})')

        return layers, cur_layer

    # Perform the forward pass
    def forward(self, x):
        """
        In forward pass, we pass input through (freezed or not) feauture extractor
        and then its output through the classifier 
        """
        representations = self.feature_extractor(x)
        classifier = self.classifier(representations)
        return torch.sigmoid(classifier) # filter it to turn output into a range between 0 and 1

### The Data Loaders ###     
    # Define functions for data loading: train / validate / test

# If you load your samples in the Dataset on CPU and would like to push it during training to the GPU, 
# you can speed up the host to device transfer by enabling "pin_memory".
# This lets your DataLoader allocate the samples in page-locked memory, which speeds-up the transfer.    
    def train_dataloader(self,train_X,train_Y):
        feature = torch.tensor(train_X.values).float() #feature tensor train_X
        target = torch.tensor(train_Y.values).float() #target tensor train_Y 
        train_dataset = TensorDataset(feature, target)  # dataset bassed on feature/target
        train_loader = DataLoader(dataset = train_dataset, 
                                  shuffle = True, 
                                  pin_memory=True if torch.cuda.is_available() else False, #for GPU
                                  num_workers = self.hparams.num_workers,
                                  batch_size = self.hparams.batch_size)
        return train_loader
            
    def test_dataloader(self,test_X,test_Y):
        feature = torch.tensor(test_X.values).float()
        target = torch.tensor(test_Y.values).float() # convert [x] -> [x,1] to match feature tensor
        test_dataset = TensorDataset(feature, target)
        test_loader = DataLoader(dataset = test_dataset, 
                                 pin_memory=True if torch.cuda.is_available() else False, #for GPU
                                 num_workers = self.hparams.num_workers,
                                 batch_size = self.hparams.batch_size)
        return test_loader

    def val_dataloader(self,validation_X,validation_Y):
        feature = torch.tensor(validation_X.values).float()
        target = torch.tensor(validation_Y.values).float()
        val_dataset = TensorDataset(feature, target)
        validation_loader = DataLoader(dataset = val_dataset,
                                       pin_memory=True if torch.cuda.is_available() else False, #for GPU
                                       num_workers = self.hparams.num_workers,
                                       batch_size = self.hparams.batch_size)
        return validation_loader

    def predict_dataloader(self):
        return self.test_dataloader()
    
### The Optimizer ### 
    # Define optimizer function: here we are using ADAM
    def configure_optimizers(self):
        return getattr(optim, self.hparams.optimizer_name)( self.parameters(),
                                                            # momentum=0.9, 
                                                            # weight_decay=1e-4,                   
                                                            lr=float(self.hparams.l_rate))

### Training ### 
    # Define training step
    def training_step(self, batch, batch_idx):
        x, y = batch
        logits = self.forward(x)
        loss = self.loss(logits, y.squeeze(1))
        # Add logging
        logs = {'loss': loss}
        self.log("loss", loss, on_epoch=True) # computes train_loss mean at end of epoch        
        return {'loss': loss, 'log': logs}

### Validation ###  
    # Define validation step
    def validation_step(self, batch, batch_idx):
        x, y = batch
        logits = self.forward(x)
        loss = self.loss(logits, y)
        self.log("val_loss", loss)
        self.log("avg_val_loss", loss)  # computes avg_loss mean at end of epoch
        print(f'loss: {loss}')
        return {'val_loss': loss}

### Testing ###     
    # Define test step
    def test_step(self, batch, batch_idx):
        x, y = batch
        logits = self.forward(x)
        loss = self.loss(logits, y)
        correct = torch.sum(logits == y.data)
        # I want to visualize my predictions vs my actuals so here I'm going to 
        # add these lines to extract the data for plotting later on
        self.log('test_loss', loss, on_epoch=True)        
        return {'test_loss': loss, 'test_correct': correct, 'logits': logits}

### Prediction ###
    # Define prediction step
        # This method takes as input a single batch of data and makes predictions on it. 
        # It then returns predictions
    def predict_step(self, batch, batch_idx):
        x, y = batch
        return torch.round(self.forward(x))
        
    def on_train_epoch_end(self):
        gc.collect()

    def on_validation_epoch_end(self):
        gc.collect()


def data_scaling(dframe, categorical_scalers=None, train_scalers=None, train=False, target=False):

    # if target dataframe, then its a single-column df, a series. Need to convert it back to df
    if(target): dframe = pd.DataFrame(dframe)

    categorical_cols = [col for col in dframe.columns if dframe[col].isin([0, 1]).all() or dframe[col].apply(lambda x: isinstance(x, str)).all()]

    # Categorical variables: label encoding
    # Initialize a dictionary to store the scalers    
    # if not training set, then use existing scalers
    if(train):
        categorical_scalers = {column: LabelEncoder() for column in categorical_cols}
        # Scale each column and store in the dictionary
        for column, scaler in categorical_scalers.items():
            if(column in dframe.columns):
                dframe[column] = scaler.fit_transform(dframe[column])
    else:
        for column, scaler in categorical_scalers.items():
            if(column in dframe.columns):
                dframe[column] = scaler.transform(dframe[column])

    # Continuous variables: scaling
    continuous_fields = [col for col in dframe.columns if col not in categorical_cols]

    # Initialize a dictionary to store the scalers      
    # if not training set, then use existing scalers
    if(train):
        train_scalers = {column: MinMaxScaler() for column in continuous_fields}
        # Scale each column and store in the dictionary
        for column, scaler in train_scalers.items():            
            if(column in dframe.columns):
                dframe[column] = scaler.fit_transform(dframe[[column]])
    else:
        for column, scaler in train_scalers.items():
            if(column in dframe.columns):
                dframe[column] = scaler.transform(dframe[[column]])

    if(train):
        return dframe, train_scalers, categorical_scalers
    else:
        return dframe

def train_test_valid_split(dframe, target_cols):
    """
    we choose to split data with validation/test data to be at the end of time series
    Parameters:
        dframe: pandas.dataframe containing dataframe to split
        stratify_cols: list of dframe cols to be used for stratification 
        target_cols: list of dframe columns to be used as target for training 
    Returns:
        pandas.dataframe containing train/test/valiation data
        pandas.dataframe containing valiation data
        pandas.dataframe containing test data
        1x2 sklearn scalers vector for X and Y respectively 
    """

    scalers = {}
    categorical_cols = [col for col in dframe.columns if dframe[col].isin([0, 1]).all() or dframe[col].apply(lambda x: isinstance(x, str)).all()]

    continuous_fields = [col for col in dframe.columns if col not in categorical_cols]

    # Remove NaNs / duplicates / outliers
    dframe = dframe.dropna().reset_index(drop=True)
    dframe.drop_duplicates(inplace=True)
    dframe = dframe[(np.abs(zscore(dframe[continuous_fields])) <= 3).all(axis=1)]

    strat_df = dframe[categorical_cols].copy()
    y = dframe[target_cols].copy()

    strat_df = dframe[categorical_cols].copy()

    dframe.drop(target_cols,axis=1,inplace=True)

    train_X, test_X, train_Y, test_Y = train_test_split(dframe, y, test_size=None, random_state=42, 
                                                        shuffle=True) #stratify=strat_df

    # train right now is both train and validation set
    train_X, scalers['X_continuous_scalers'], scalers['X_categorical_scalers'] = data_scaling(train_X, train=True)
    train_Y, scalers['Y_continuous_scalers'], scalers['Y_categorical_scalers'] = data_scaling(train_Y, train=True, target=True)

    test_X = data_scaling(test_X, scalers['X_categorical_scalers'], scalers['X_continuous_scalers'])
    test_Y = data_scaling(test_Y, scalers['Y_categorical_scalers'], scalers['Y_continuous_scalers'], target=True)

    train_stratify_cols = [item for item in train_X.columns if item in categorical_cols]
    strat_df = train_X[train_stratify_cols].copy()
    
    train_X, validation_X, train_Y, validation_Y = train_test_split(train_X, train_Y, test_size=0.25, random_state=42, 
                                                      shuffle=True) # , stratify=train_Y

    return train_X, validation_X, test_X, train_Y, validation_Y, test_Y, scalers

def cross_plot_actual_pred(plot_pred, plot_actual):
    # And finally we can see that our network has done a decent job of estimating!
    fig, ax = plt.subplots(figsize=(16,4))
    ax.plot(plot_pred, label='Prediction')
    ax.plot(plot_actual, label='Data')
    ax.legend()
    plt.show()
    if not os.path.exists("./plots/"): os.makedirs("./plots/")
    plt.savefig('./plots/pred_actual.png')

def calculate_metrics(test_X, test_Y, model_path):
    """
    Returns classification metrics based on the true and predicted labels as a dictionary.
    
    Parameters:
    - test_X: array-like of shape (n_samples,) - True labels.
    - test_Y: array-like of shape (n_samples,) - Predicted labels.
    
    Returns:
    - A dictionary with accuracy, precision, recall, f1 score, and confusion matrix.
    """

    model = Classifier.load_from_checkpoint(checkpoint_path=model_path)
    test_X_tensor = torch.tensor(test_X.values, dtype=torch.float32)
    test_Y_tensor = torch.tensor(test_Y.values, dtype=torch.float32)
    pred_Y = model(test_X_tensor).round() 
    
    # Define classification metrics
    accuracy = MultilabelAccuracy(num_labels=4)
    precision = MultilabelPrecision(num_labels=4, average='macro')
    recall = MultilabelRecall(num_labels=4, average='macro')
    f1 = MultilabelF1Score(num_labels=4, average='macro')
    ham = MultilabelHammingDistance(num_labels=4, average='macro')

    metrics = {
        'accuracy': accuracy(pred_Y, test_Y_tensor).item(),
        'precision': precision(pred_Y, test_Y_tensor).item(),
        'recall': recall(pred_Y, test_Y_tensor).item(),
        'f1_score': f1(pred_Y, test_Y_tensor).item(),
        'hamming': ham(pred_Y, test_Y_tensor).item()
    }
    
    print("  Metrics: ")
    for key, value in metrics.items():
        print("    {}: {}".format(key, value))

    
    return metrics

def keep_best_model_callback(study, trial):
    if study.best_trial.number == trial.number:
        study.set_user_attr(key="best_trainer", value=trial.user_attrs["best_trainer"])

def objective(trial, kwargs, df, num_target_variables):
    """
    Function used by optuna for hyperparameter tuning
    Each execution of this function is basically a new trial with its params
    changed as suggest by suggest_* commands
    Parameters: 
        trial object (default)
    Returns: 
        validation loss of model used for checking progress of tuning 
    """

    n_layers = list(map(int, kwargs['n_layers'].split(',')))
    l_rate = list(map(float, kwargs['l_rate'].split(','))) 
    layer_sizes = list(map(int, kwargs['layer_sizes'].split(',')))

    n_layers = trial.suggest_int("n_layers", n_layers[0], n_layers[1])
    params = {
        'input_dim': len(df.columns) - num_target_variables, # df also has target column
        'max_epochs': kwargs['max_epochs'],
        'seed': 42,
        'layer_sizes': [trial.suggest_categorical("n_units_l{}".format(i), layer_sizes) for i in range(n_layers)], 
        'l_rate':  trial.suggest_float('l_rate', l_rate[0], l_rate[1], log=True), # loguniform will become deprecated
        'activation': trial.suggest_categorical("activation", kwargs['activation']) if ',' in kwargs['activation'] else kwargs['activation'], #SiLU (Swish) performs good
        'optimizer_name': trial.suggest_categorical("optimizer_name", kwargs['optimizer_name']) if ',' in kwargs['optimizer_name'] else kwargs['optimizer_name'],
        'batch_size': int(trial.suggest_categorical('batch_size', kwargs['batch_size'].split(','))),
        'num_workers': int(kwargs['num_workers'])
    }

    print(params)
    
    # ~~~~~~~~~~~~~~ Setting up network ~~~~~~~~~~~~~~~~~~~~~~
    torch.set_num_threads(params['num_workers']) 
    pl.seed_everything(params['seed'], workers=True)  

    model = Classifier(**params) # double asterisk (dictionary unpacking)

    trainer = Trainer(max_epochs=int(params['max_epochs']), deterministic=True,
                      accelerator='auto', 
                    #   devices = 1 if torch.cuda.is_available() else 0,
                    # auto_select_gpus=True if torch.cuda.is_available() else False,
                    callbacks=[PyTorchLightningPruningCallback(trial, monitor="val_loss"),
                               EarlyStopping(monitor="val_loss", mode="min", verbose=True)]) 

    trainer.logger.log_hyperparams(params)

    train_loader = model.train_dataloader(train_X,train_Y)
    val_loader = model.val_dataloader(validation_X,validation_Y)

    print("############################ Traim/Test/Validate ###############################")
    
    trainer.fit(model, train_loader, val_loader)

    # store each trial trainer and update it at objetive's callback function to keep best
    trial.set_user_attr(key="best_trainer", value=trainer)

    return trainer.callback_metrics["val_loss"].item()
                
#  Example taken from Optuna github page:
# https://github.com/optuna/optuna-examples/blob/main/pytorch/pytorch_lightning_simple.py

# Theory:
# https://coderzcolumn.com/tutorials/machine-learning/simple-guide-to-optuna-for-hyperparameters-optimization-tuning

                
def print_optuna_report(study):
    """
    This function prints hyperparameters found by optuna
    """    
    warnings.filterwarnings("ignore", ".*Consider increasing the value of the `num_workers` argument*")

    print("\n ~~~~~~~~~~~~~~~~~~~~~~~~~~ Optuna Report ~~~~~~~~~~~~~~~~~~~~~~~~~~")
    print("Number of finished trials: {}".format(len(study.trials)))

    print("Best trial:")
    trial = study.best_trial

    print("  Value: {}".format(trial.value))
    print(" Number: {}".format(trial.number))
    print("  Params: ")
    for key, value in trial.params.items():
        print("    {}: {}".format(key, value))

# The pruners module defines a BasePruner class characterized by an abstract prune() method, which, 
# for a given trial and its associated study, returns a boolean value 
# representing whether the trial should be pruned (aborted)
# optuna.pruners.MedianPruner() 
# optuna.pruners.NopPruner() (no pruning)
# Hyperband performs best with default sampler for non-deep learning tasks
def optuna_optimize(kwargs, df, num_target_variables):
    """
    Function used to setup optuna for study
    Parameters: None
    Returns: study object containing info about trials
    """
    # The pruners module defines a BasePruner class characterized by an abstract prune() method, which, 
    # for a given trial and its associated study, returns a boolean value 
    # representing whether the trial should be pruned (aborted)
    warnings.filterwarnings("ignore", ".*Consider increasing the value of the `num_workers` argument*")

    # optuna.pruners.MedianPruner() 
    # optuna.pruners.NopPruner() (no pruning)
    # Hyperband performs best with default sampler for non-deep learning tasks
    pruner: optuna.pruners.BasePruner = optuna.pruners.MedianPruner() # experimentally better performance
        
    # default sampler: TPMESampler    
    study = optuna.create_study(direction="minimize", pruner=pruner)
    """
    timeout (Union[None, float]) – Stop study after the given number of second(s). 
    None represents no limit in terms of elapsed time. 
    The study continues to create trials until: the number of trials reaches n_trials, 
                                                timeout period elapses, stop() is called or, 
                                                a termination signal such as SIGTERM or Ctrl+C is received.
    """
    study.optimize(lambda trial: objective(trial, kwargs, df, num_target_variables),
                #  n_jobs=2,
                #    timeout=600, # 10 minutes
                   callbacks=[keep_best_model_callback],
                   n_trials=kwargs['n_trials'],
                   gc_after_trial=True)
    
    print_optuna_report(study)
    
    return study

def optuna_visualize(study, optuna_viz):

    # if dir does not exit, make it
    if not os.path.exists(optuna_viz): os.makedirs(optuna_viz)

    plt.close() # close any mpl figures (important, doesn't work otherwise)
    optuna.visualization.matplotlib.plot_param_importances(study)
    plt.savefig(f"{optuna_viz}/plot_param_importances.png"); plt.close()

    optuna.visualization.matplotlib.plot_optimization_history(study)
    plt.savefig(f"{optuna_viz}/plot_optimization_history.png"); plt.close()
    
    optuna.visualization.matplotlib.plot_slice(study)
    plt.savefig(f"{optuna_viz}/plot_slice.png"); plt.close()

    optuna.visualization.matplotlib.plot_intermediate_values(study)
    plt.savefig(f"{optuna_viz}/plot_intermediate_values.png"); plt.close()

def service_1_model_predict(test_X, service_1_targets, model_path, scalers_path):
    model = Classifier.load_from_checkpoint(checkpoint_path=model_path)

    with open('./models-scalers/MLPClassifier_scalers.pkl', 'rb') as f: scalers = pickle.load(f)

    # with open(scalers_path, 'rb') as f: scalers = pickle.load(f)

    print(test_X)

    test_X = pd.DataFrame.from_dict(test_X)

    test_X = data_scaling(test_X, scalers['X_categorical_scalers'], scalers['X_continuous_scalers'])

    test_X_tensor = torch.tensor(test_X[:10].values, dtype=torch.float32)

    pred_Y = model(test_X_tensor).round().tolist()
    print(pred_Y)
    
    pred_dict = {service_1_targets[i]: int(value) for i, value in enumerate(pred_Y[0])}

    print(pred_dict)
    
    return pred_dict

def argument_extraction(kwargs):
    print("############################ Reading Data ###############################")
    df = pd.read_csv(kwargs['input_filepath']) #,index_col=0

    print("############################ Data Preprocess ###############################")
        # Remove date column (not needed)
    feature_cols = kwargs['feature_cols'].split(",") #'The data,Primary energy consumption after ,Reduction of primary energy,CO2 emissions reduction'
        
    if(',' in kwargs['target_cols']): # if multiple targets
        target_cols = kwargs['target_cols'].split(",")
    else:
        target_cols = [kwargs['target_cols']]

    df = df[feature_cols + target_cols]
    
    # find categorical columns (string-based) 
    categorical_cols = [col for col in df.columns if df[col].isin([0, 1]).all() or df[col].apply(lambda x: isinstance(x, str)).all()]

    return df, target_cols, categorical_cols

# Remove whitespace from your arguments
@click.command(
    help= "Given a folder path for CSV files (see load_raw_data), use it to create a model, find\
            find ideal hyperparameters and train said model to reduce its loss function"
)

@click.option("--input_filepath", type=str, default='./datasets/EF_comp.csv', help="File containing csv files used by the model")
@click.option("--seed", type=int, default=42, help='seed used to set random state to the model')
@click.option("--max_epochs", type=int, default=5, help='range of number of epochs used by the model')
@click.option("--n_layers", type=str, default='2,6', help='range of number of layers used by the model')
@click.option("--layer_sizes", type=str, default="128,256,512,1024,2048", help='range of size of each layer used by the model')
@click.option("--l_rate", type=str, default='0.0001,0.001', help='range of learning rate used by the model')
@click.option("--activation", type=str, default="ReLU", help='activations function experimented by the model')
@click.option("--optimizer_name", type=str, default="Adam", help='optimizers experimented by the model') # SGD
@click.option("--batch_size", type=str, default='256,512,1024', help='possible batch sizes used by the model') #16,32,
@click.option("--num_workers", type=int, default=2, help='accelerator (cpu/gpu) processesors and threads used') 
@click.option('--n_trials', type=int, default=3, help='number of trials for HPO')
@click.option('--preprocess', type=int, default=1, help='data preprocessing and scaling')
@click.option('--feature_cols', type=str, default='Building total area,Reference area,Above ground floors,Underground floor,Initial energy class,Energy consumption before,Energy class after', help='Dataset columns not necesary for training')
@click.option('--target_cols', type=str, default='Carrying out construction works,Reconstruction of engineering systems,Heat installation,Water heating system', help='Target column that we want to predict (model output)')
@click.option('--predict', type=int, default=0, help='predict value or not')
@click.option('--model_path', type=str, default='./backup-models-scalers/best_MLPClassifier.ckpt', help='directory to store models and scalers')
@click.option('--scalers_path', type=str, default='./backup-models-scalers/MLPClassifier_scalers.pkl', help='filename of best model')

def forecasting_model(**kwargs):
    """
    This is the main function of the script. 
    1. It loads the data from csvs
    2. splits to train/test/valid
    3. trains model based on hyper params set
    4. computes MAPE and plots graph of best model
    Parameters
        kwargs: dictionary containing click paramters used by the script
    Returns: None 
    """
    # ~~~~~~~~~~~~ Data Collection & process ~~~~~~~~~~~~~~~~~~~~
    df, target_cols, categorical_cols = argument_extraction(kwargs)

    global train_X, validation_X, test_X, train_Y, validation_Y, test_Y, scalers
    train_X, validation_X, test_X, train_Y, validation_Y, test_Y, scalers = train_test_valid_split(df,target_cols)
    with open(kwargs['scalers_path'], 'wb') as f: pickle.dump(scalers, f)

    if(kwargs['predict']):
        service_1_model_predict(test_X, target_cols, categorical_cols, kwargs['model_path'], kwargs['scalers_path'])
        return
    
    study = optuna_optimize(kwargs, df, len(target_cols))
    
    # visualize results of study
    optuna_visualize(study, './optuna_viz/classifier/')

    # if not os.path.exists(kwargs["model_path"]): os.makedirs(kwargs["model_path"])

    print(f'Save best model at file: \"{kwargs["model_path"]}\"')
    best_model = study.user_attrs["best_trainer"]
    best_model.save_checkpoint(kwargs["model_path"])
    print(f'Save data scalers at files: \"categorical_scalers\" and \"train_scalers\"')
    
    calculate_metrics(test_X, test_Y, kwargs["model_path"])


if __name__ == '__main__':
    print("\n=========== Forecasing Model =============")
    logging.info("\n=========== Forecasing Model =============")
    forecasting_model()