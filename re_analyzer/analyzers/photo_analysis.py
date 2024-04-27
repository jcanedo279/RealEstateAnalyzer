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

from re_analyzer.utility.utility import DATA_PATH, ensure_directory_exists, save_json
from re_analyzer.analyzers.preprocessing import load_data, preprocess_dataframe, FilterMethod
from re_analyzer.analyzers.iterator import property_details_iterator, get_property_info_from_property_details


combined_df = load_data()
preprocessed_df, preprocessor = preprocess_dataframe(combined_df, filter_method=FilterMethod.FILTER_P_SCORE)

BATCH_SIZE = 32
NUM_BATCHES = 250
NUM_EPOCHS = 10
DATASET_SIZE = min(NUM_BATCHES * BATCH_SIZE, len(preprocessed_df)-1)
inv_preprocessed_df = preprocessor.inverse_transform(preprocessed_df)
training_df = inv_preprocessed_df[:DATASET_SIZE]

IMAGE_DIR = f'{DATA_PATH}/Images'
ensure_directory_exists(IMAGE_DIR)

print("Starting to query images")
for zpid, property in training_df.iterrows():
    zip_code, zpid = int(property['zip_code']), int(zpid)
    property_im = f'{IMAGE_DIR}/zpid_{zpid}_im.png'
    if os.path.exists(property_im):
        continue
    property_details_path = f'{DATA_PATH}/PropertyDetails/{zip_code}/{zpid}_property_details.json'
    if not os.path.exists(property_details_path):
        training_df.drop(zpid, inplace=True)
        continue
    with open(property_details_path) as property_file:
        property_details = json.load(property_file)
        # Yield the loaded JSON data
        if 'props' not in property_details:
            training_df.drop(zpid, inplace=True)
            continue
        property_info = get_property_info_from_property_details(property_details)
        if not property_info['originalPhotos']:
            training_df.drop(zpid, inplace=True)
            continue
        image_url = property_info['originalPhotos'][0]['mixedSources']['jpeg'][1]['url']

        # Send a GET request to the image URL
        response = requests.get(image_url)

        # Check if the request was successful
        if response.status_code == 200:
            # Open the specified path in binary write mode and save the image
            with open(property_im, "wb") as file:
                file.write(response.content)
print("Image collection complete")

# Apply pd.cut to categorize purchase_price into bins within each zip_code group
NUM_BINS = 3
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
    def __init__(self, num_classes=NUM_BINS):
        super(CNNModel, self).__init__()
        # Define the first convolutional layer
        self.conv1 = nn.Conv2d(3, 16, kernel_size=3, stride=1, padding=1)
        # Define the second convolutional layer
        self.conv2 = nn.Conv2d(16, 32, kernel_size=3, stride=1, padding=1)
        # Define the third convolutional layer
        self.conv3 = nn.Conv2d(32, 64, kernel_size=3, stride=1, padding=1)
        # Define a max pooling layer
        self.pool = nn.MaxPool2d(kernel_size=2, stride=2, padding=0)
        # Dropout layer with a 50% drop probability
        # self.dropout = nn.Dropout(0.10)
        # Define a fully connected layer
        self.fc1 = nn.Linear(64 * 28 * 28, 512)
        # self.fc2 = nn.Linear(512, 1)  # Output layer for regression task
        self.fc2 = nn.Linear(512, num_classes)

    def forward(self, x):
        # Apply the first convolutional layer followed by ReLU and max pooling
        x = self.pool(F.relu(self.conv1(x)))
        # Apply the second convolutional layer followed by ReLU and max pooling
        x = self.pool(F.relu(self.conv2(x)))
        # Apply the third convolutional layer followed by ReLU and max pooling
        x = self.pool(F.relu(self.conv3(x)))
        # Apply dropout
        # x = self.dropout(x)
        # Flatten the output for the fully connected layer
        x = x.view(x.size(0), -1)
        # Apply the first fully connected layer followed by ReLU
        x = F.relu(self.fc1(x))
        # Apply the second fully connected layer to produce the output
        x = self.fc2(x)
        return x


# Instantiate the model, loss criterion, and optimizer
model = CNNModel(num_classes=NUM_BINS)
# criterion = nn.MSELoss() # Used for regression loss
criterion = nn.CrossEntropyLoss() # Used for categorical loss
optimizer = torch.optim.Adam(model.parameters(), lr=0.001, weight_decay=1e-5)

transform_train = transforms.Compose([
    # transforms.RandomHorizontalFlip(),
    # transforms.RandomAffine(0, shear=10, scale=(0.8, 1.2)),
    # transforms.ColorJitter(brightness=0.2, contrast=0.2, saturation=0.2),
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


best_val_loss = float('inf')

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

    print(f'Epoch [ {epoch+1} | {NUM_EPOCHS} ], Train Loss: {train_loss:.4f}, Val Loss: {val_loss:.4f}')

    # Save the model with the best validation loss
    if val_loss < best_val_loss:
        best_val_loss = val_loss
        torch.save(model.state_dict(), 'best_model.pth')

