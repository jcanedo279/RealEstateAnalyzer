import os
import requests
import json

import torch
import torch.nn as nn
import torch.nn.functional as F
import pandas as pd
from torch.utils.data import Dataset, DataLoader
from torchvision import transforms
from PIL import Image
from sklearn.model_selection import train_test_split
from itertools import product

from re_analyzer.utility.utility import DATA_PATH, ensure_directory_exists, save_json
from re_analyzer.analyzers.preprocessing import load_data, preprocess_dataframe, FilterMethod
from re_analyzer.analyzers.iterator import property_details_iterator, get_property_info_from_property_details

NUM_EPOCHS = 7
NUM_BINS = 3
IMAGE_DIR = f'{DATA_PATH}/Images'
ensure_directory_exists(IMAGE_DIR)

combined_df = load_data()
preprocessed_df, preprocessor = preprocess_dataframe(combined_df, filter_method=FilterMethod.FILTER_P_SCORE)

inv_preprocessed_df = preprocessor.inverse_transform(preprocessed_df)

class PropertyImageDataset(Dataset):
    def __init__(self, X, y, zpids, transform=None, mode='train'):
        self.X = X
        self.transform = transform
        self.mode = mode
        self.image_files = [f'{IMAGE_DIR}/zpid_{zpid}_im.png' for zpid in zpids if os.path.isfile(f'{IMAGE_DIR}/zpid_{zpid}_im.png')]
        self.zpid_to_target = {zpid: y[zpid] for zpid in zpids}

    def __len__(self):
        return len(self.image_files)

    def __getitem__(self, idx):
        img_name = self.image_files[idx]
        zpid = int(img_name.split('_')[1])
        image = Image.open(img_name).convert('RGB')
        
        if self.transform:
            image = self.transform(image)
        
        target = self.zpid_to_target[zpid]
        
        return image, target
    

class CNNModel(nn.Module):
    def __init__(self, conv_layers_sizes, fc_layers_sizes, num_classes=NUM_BINS, dropout_prob=0.5):
        super(CNNModel, self).__init__()
        # Initialize convolutional layers dynamically
        self.conv_layers = nn.ModuleList()
        in_channels = 3  # Initial number of input channels (for RGB images)
        for out_channels, kernel_size in conv_layers_sizes:
            self.conv_layers.append(
                nn.Sequential(
                    nn.Conv2d(in_channels, out_channels, kernel_size=kernel_size, stride=1, padding=kernel_size//2),
                    nn.ReLU(),
                    nn.MaxPool2d(kernel_size=2, stride=2)
                )
            )
            in_channels = out_channels  # Update in_channels for the next layer
        
        # Calculate the size of the flattened features after the last conv layer
        self.num_flat_features = in_channels * (224 // 2**len(conv_layers_sizes))**2  # Assuming input image size of 224x224

        # Initialize fully connected layers dynamically
        self.fc_layers = nn.ModuleList()
        in_features = self.num_flat_features  # Input size for the first fc layer
        for fc_layer_size in fc_layers_sizes[:-1]:  # Exclude the last layer size, it will be handled separately
            self.fc_layers.append(
                nn.Sequential(
                    nn.Linear(in_features, fc_layer_size),
                    nn.ReLU(),
                    nn.Dropout(dropout_prob)
                )
            )
            in_features = fc_layer_size  # Update in_features for the next layer
        
        # The last fully connected layer (without dropout)
        self.fc_layers.append(nn.Linear(in_features, num_classes))

    def forward(self, x):
        # Pass input through convolutional layers
        for conv_layer in self.conv_layers:
            x = conv_layer(x)
        
        # Flatten the output for the fully connected layer
        x = x.view(-1, self.num_flat_features)
        
        # Pass input through fully connected layers
        for i, fc_layer in enumerate(self.fc_layers):
            x = fc_layer(x) if i < len(self.fc_layers) - 1 else self.fc_layers[-1](x)  # Apply activation only if it's not the last layer
        
        return x



def photo_analysis_with_hyp(hyper_parameters):
    BATCH_SIZE = hyper_parameters['batch_size']
    NUM_BATCHES = min(10000 // BATCH_SIZE, 150)
    DATASET_SIZE = min(NUM_BATCHES * BATCH_SIZE, len(preprocessed_df)-1)
    training_df = inv_preprocessed_df[:DATASET_SIZE]

    # Apply pd.cut to categorize purchase_price into bins within each zip_code group
    inv_preprocessed_df['price_bin'] = inv_preprocessed_df.groupby('zip_code')['purchase_price'].transform(
        lambda x: pd.cut(x, bins=NUM_BINS, labels=False)
    )
    training_df.loc[:, 'price_bin'] = inv_preprocessed_df.loc[training_df.index, 'price_bin'].values

    # Split the training dataframe into training and validation datasets
    X_train, X_val, y_train, y_val = train_test_split(training_df.drop(columns=['price_bin']), 
                                                    training_df['price_bin'], 
                                                    test_size=0.2, 
                                                    random_state=42)
    # Create sets of zpids for training and validation
    train_zpids = X_train.index
    val_zpids = X_val.index


    # Instantiate the model, loss criterion, and optimizer
    model = CNNModel(
        conv_layers_sizes=hyper_parameters['conv_layer_sizes'],
        fc_layers_sizes=hyper_parameters['fc_layer_sizes'],
        dropout_prob=hyper_parameters['dropout_rate']
    )
    # criterion = nn.MSELoss() # Used for regression loss
    criterion = nn.CrossEntropyLoss() # Used for categorical loss
    optimizer = torch.optim.Adam(model.parameters(), lr=hyper_parameters['learning_rate'], weight_decay=1e-5)

    transform_train = transforms.Compose([
        transforms.RandomHorizontalFlip(),
        transforms.ColorJitter(brightness=0.2, contrast=0.2, saturation=0.2),
        transforms.Resize((224, 224)),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])

    transform_val = transforms.Compose([
        transforms.Resize((224, 224)),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])


    # Instantiate datasets for training and validation
    train_dataset = PropertyImageDataset(X_train, y_train, train_zpids, transform=transform_train, mode='train')
    val_dataset = PropertyImageDataset(X_val, y_val, val_zpids, transform=transform_val, mode='val')

    # DataLoaders
    train_loader = DataLoader(train_dataset, batch_size=BATCH_SIZE, shuffle=True, drop_last=True)
    val_loader = DataLoader(val_dataset, batch_size=BATCH_SIZE, shuffle=False, drop_last=True)


    best_train_loss_train, best_train_loss_val = float('inf'), float('inf')
    best_val_loss_train, best_val_loss_val = float('inf'), float('inf')

    # Training and validation.
    for epoch in range(NUM_EPOCHS):
        # Training phase
        model.train()  # Set model to training mode
        train_loss = 0.0
        for images, targets in train_loader:
            # Forward pass
            outputs = model(images)
            loss = criterion(outputs, targets.long())  # Ensure your targets are long type for CrossEntropyLoss

            # Backward and optimize
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            train_loss += loss.item() * images.size(0)

        train_loss = train_loss / len(train_loader.dataset)

        # Validation phase
        model.eval()  # Set model to evaluation mode
        val_loss = 0.0
        with torch.no_grad():  # No need to track gradients during validation
            for images, targets in val_loader:
                # Forward pass
                outputs = model(images)
                loss = criterion(outputs, targets.long())

                val_loss += loss.item() * images.size(0)

        val_loss = val_loss / len(val_loader.dataset)

        hyp_string = ", ".join([f"{key}: {value}" for key, value in hyper_parameter.items()])
        print(f'Epoch [ {epoch+1} | {NUM_EPOCHS} ], Train Loss: {train_loss:.4f}, Val Loss: {val_loss:.4f} --> {hyp_string}')

        if train_loss < best_train_loss_train:
            best_train_loss_train = train_loss
            best_train_loss_val = val_loss
        if val_loss < best_val_loss_val:
            best_val_loss_val = val_loss
            best_val_loss_train = train_loss
    
    return (best_train_loss_train, best_train_loss_val), (best_val_loss_train, best_val_loss_val)


hyper_parameters = {
    'batch_size': [32, 64, 128],
    'learning_rate': [0.001, 0.0005, 0.0001],
    'dropout_rate': [0, 0.25, 0.5],
    'conv_layer_sizes': [
        [(16, 3), (32, 3)],
        [(32, 3), (64, 3)],
        [(32, 3), (64, 3), (128, 3)]
    ],
    'fc_layer_sizes': [
        [256, NUM_BINS],
        [512, 256, NUM_BINS],
        [512, 512, NUM_BINS]
    ]
}

hyper_parameter_combinations = [dict(zip(hyper_parameters.keys(), combination)) for combination in product(*hyper_parameters.values())]
hyp_photo_analysis_results_path = 'hyp_results.txt'

# Read the results file and parse each line as JSON
existing_hyp_combinations = set()
with open(hyp_photo_analysis_results_path, 'r') as file:
    for line in file:
        existing_hyp_combinations.add(json.loads(line)['hyp_comb'])

with open(hyp_photo_analysis_results_path, 'a') as results_file:
    for hyper_parameter in hyper_parameter_combinations:
        hyper_parameter_string = ", ".join([f"{key}: {value}" for key, value in hyper_parameter.items()])

        if hyper_parameter_string in existing_hyp_combinations:
            continue

        (best_train_loss_train, best_train_loss_val), (best_val_loss_train, best_val_loss_val) = photo_analysis_with_hyp(hyper_parameter)
        hyp_search_result = {
            'hyp_comb': hyper_parameter_string,
            'train_loss_train_val': f"{best_train_loss_train}, {best_train_loss_val}",
            'val_loss_train_val': f"{best_val_loss_train}, {best_val_loss_val}"
        }
        results_file.write(json.dumps(hyp_search_result) + '\n')
