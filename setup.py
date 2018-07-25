# coding=utf-8

import json
import numpy as np
import os
import pandas as pd
import re
import torch
import torch.nn as nn
from gensim.models import KeyedVectors
from gensim.models import FastText
from nltk.corpus import stopwords
from torch.utils.data import TensorDataset
from tqdm import tqdm

from model.attention.abcnn1 import ABCNN1Attention
from model.attention.abcnn2 import ABCNN2Attention
from model.blocks.abcnn1 import ABCNN1Block
from model.blocks.abcnn2 import ABCNN2Block
from model.blocks.abcnn3 import ABCNN3Block
from model.blocks.bcnn import BCNNBlock
from model.convolution.conv import Convolution
from model.model import Model
from model.layers.layer import CNNLayer
from model.pooling.allap import AllAP
from model.pooling.widthap import WidthAP
from process import setup_dataset

# Use GPU if available, otherwise use CPU
USE_CUDA = torch.cuda.is_available()


def read_config(config_path):
    """ Reads in the configuration file from the given path.

        Args:
            config_path: string
                The path to the configuration file.

        Returns:
            config: dict
                Contains the information needed to initialize the
                datasets and model. See "config.json" for configuration
                details.
    """
    with open(config_path) as json_file:
        config = json.load(json_file)
        return config


def setup(config):
    """ Handles all of the setup needed to run an ABCNN model.

        Args:
            config: dict
                Contains the information needed to initialize the datasets
                and model.

        Returns:
            datasets: dict of TensorDatasets
                Contains the datasets to be used for training/evaluation.
            model: Model
                The instantiated model.
    """
    # Read in the relevant config info
    data_paths = config["data_paths"]
    embeddings_size = config["embeddings"]["size"]
    max_length = config["model"]["max_length"]

    # Setup the datasets
    datasets = {name: pd.read_csv(data_path) for name, data_path in data_paths.items()}
    datasets, texts, word2index = setup_datasets(datasets, embeddings_size, max_length)

    # Create embedding matrix
    word_vectors = setup_word_vectors(config)
    embeddings = setup_embedding_matrix(word_vectors, word2index, embeddings_size)
    
    # Create the model
    model = setup_model(embeddings, config)
    return datasets, model


def setup_model(embeddings, config):
    """ Sets up the model for training/evaluation. The architecture here extends
        on the architecture introduced in the ABCNN paper by allowing for multiple
        convolutional layers with different window sizes (computed in parallel, not
        in series).

        Args:
            config: dict
                Contains the information needed to setup the model.

        Returns:
            model: ModelV2 module
                The instantiated model.
    """
    print("Creating the ABCNN model...")

    # Get relevant parameters
    embeddings_size = config["embeddings"]["size"]
    max_length = config["model"]["max_length"]
    layer_configs = config["model"]["layers"]
    use_all_layer_outputs = config["model"]["use_all_layer_outputs"]

    # Create the layers
    layers = []
    layer_sizes = [embeddings_size]
    for layer_config in layer_configs:
        layer, layer_size = setup_layer(max_length, layer_config)
        layers.append(layer)
        layer_sizes.append(layer_size)

    # Compute the size of the FC layer
    final_size = 2 * sum(layer_sizes) if use_all_layer_outputs else 2 * layer_sizes[-1]

    # Put it all together
    model = Model(embeddings, layers, use_all_layer_outputs, final_size).float()
    model = model.cuda() if USE_CUDA else model
    model.apply(weights_init)
    return model


def setup_word_vectors(config):
    """ Loads the pre-trained word vectors. The word vector file can be in
        Word2Vec or FastText formats.

        Args:
            config: dict
                Contains the information needed to initialize the embeddings
                model.

        Returns:
            word_vectors: KeyedVectors, FastTextKeyedVectors, or None
                The pretrained word embeddings. If the embeddings path
                is for a pre-trained Word2Vec model, then a KeyedVectors
                instance is returned. If the embeddings path is for a
                pre-trained FastText model, then a FastTextKeyedVectors
                instance is returned. Otherwise, None is returned.
    """
    # Get relevant parameters from config file
    embeddings_path = config["embeddings"]["path"]
    embeddings_format = config["embeddings"]["format"]
    is_binary = config["embeddings"]["is_binary"]
   
    # Load pre-trained word embeddings
    word_vectors = None
    if embeddings_format == "word2vec":
        if os.path.isfile(embeddings_path):
            print("Loading Word2Vec word vectors from: {}".format(embeddings_path))
            word_vectors = KeyedVectors.load_word2vec_format(embeddings_path, binary=is_binary)
    elif embeddings_format == "fasttext":
        if os.path.isfile(embeddings_path):
            print("Loading FastText word vectors from: {}".format(embeddings_path))
            embeddings_model = FastText.load_fasttext_format(embeddings_path)
            word_vectors = embeddings_model.wv 
    else:
        raise Exception("Unsupported type. Must be one of 'word2vec' or 'fasttext'.")
    return word_vectors


def setup_datasets(datasets, embeddings_size, max_length):
    """ Converts the examples from the datasets into a machine-readable format
        useful for training.

        To ensure that all words have a word embedding associated to them, we 
        should have text from ALL datasets (note: this is NOT peeking at the 
        dataset... this is just to prevent the model from crashing/complaining 
        when it sees a word that is OOV.) OOV words are assigned random word 
        embeddings.

        Args:
            datasets: dict of pd.DataFrame
                The text we would like to convert to indices into the embedding
                matrix.
            word2index: dict
                Contains the mapping from words to their indices in the
                embedding matrix.
            word_vectors: KeyedVectors, FastTextKeyedVectors, or None
                Contains the pre-trained word vectors, if available.
            embeddings_size: int
                The dimension of the word embeddings.
            max_length: int
                The maximum length of questions/sentences.

        Returns:
            index_mappings: dict of torch.LongTensors
                The keys are the names of the datasets. The values are
                LongTensors of shape (num_examples, 2, max_length).
                Each example is represented as a LongTensor of indices
                into the embedding matrix.
            word2index: dict
                Maps each word to an index of the embedding matrix.
            index2word: dict
                Maps each index of the embedding matrix to a word.
    """
    texts = dict()
    word2index = {"<PAD>": 0}
    question_cols = ["question1", "question2"]

    # Process each dataset
    for name, dataset in datasets.items():
        
        # Process texts
        labels = []
        indexed_examples = []
        parsed_texts = []
        num_examples = len(dataset)
        for index, example in tqdm(dataset.iterrows(), desc=name, total=num_examples):

            # Process each question separately
            index_map = []
            parsed_text = []
            for column in question_cols:

                # Parse and clean the text
                question = example[column]
                words = text_to_word_list(question)
                words = remove_stop_words(words)

                # Convert words to indices
                indexes = []
                for word in words:

                    # Update word-index lookup if necessary
                    if word not in word2index:
                        word2index[word] = len(word2index)
                    
                    # Add the word's index to the list
                    indexes.append(word2index[word])

                # Truncate if necessary
                length = len(indexes) if len(indexes) < max_length else max_length
                indexes = indexes[:length]
                words = words[:length]

                # Pad if necessary
                if length < max_length:
                    num_padding = max_length - length
                    indexes.extend([0] * num_padding)
                    words.extend(["<PAD>"] * num_padding)
    
                # Store parsed text and index tensors
                index_map.append(indexes)
                parsed_text.append(words)

            # Store processed text and index tensor map and label
            labels.append(example["is_duplicate"])
            indexed_examples.append(index_map)
            parsed_texts.append(parsed_text)

        # Save the processed result
        labels = torch.LongTensor(labels)
        indexed_examples = torch.LongTensor(indexed_examples)
        dataset = TensorDataset(indexed_examples, labels)
        datasets[name] = dataset
        texts[name] = parsed_texts

    return datasets, texts, word2index
   

def setup_embedding_matrix(word_vectors, word2index, embeddings_size):
    """ Creates the embedding matrix using the given word embeddings and mapping
        from words to indices.

        Args:
            word_vectors: KeyedVectors, FastTextKeyedVectors, or None
                The pre-trained word-vectors, if available.
            word2index: dict
                Maps words to indices in the embedding matrix.

        Returns
            embeddings: nn.Embedding
                The embedding matrix.
    """
    # Sanity check
    embeddings = np.random.uniform(-0.01, 0.01, (len(word2index) + 1, embeddings_size))
    embeddings[0] = 0   # Padding is just all 0s

    # Replace random vectors with pre-trained vectors if available
    if word_vectors is not None:
        for word, index in tqdm(word2index.items(), desc="embedding matrix"):
            try:
                embeddings[index] = word_vectors[word]
            except (RuntimeError, KeyError):
                pass

    # Convert to nn.Embedding
    embeddings = nn.Embedding.from_pretrained(torch.from_numpy(embeddings))
    return embeddings
    

def text_to_word_list(text):
    """ Preprocess and convert texts to a list of words. This code was taken 
        from Elior Cohen's MaLSTM code, which can be found here:

        https://github.com/eliorc/Medium/blob/master/MaLSTM.ipynb
        
         Args:
             text: string
                 The text to parse.

         Returns:
             text: list of string
                 The parsed text.
     """
    text = str(text)
    text = text.lower()

    # Clean the text
    text = re.sub(r"[^A-Za-z0-9^,!.\/'+-=]", " ", text)
    text = re.sub(r"what's", "what is ", text)
    text = re.sub(r"\'s", " ", text)
    text = re.sub(r"\'ve", " have ", text)
    text = re.sub(r"can't", "cannot ", text)
    text = re.sub(r"n't", " not ", text)
    text = re.sub(r"i'm", "i am ", text)
    text = re.sub(r"\'re", " are ", text)
    text = re.sub(r"\'d", " would ", text)
    text = re.sub(r"\'ll", " will ", text)
    text = re.sub(r",", " ", text)
    text = re.sub(r"\.", " ", text)
    text = re.sub(r"!", " ! ", text)
    text = re.sub(r"\/", " ", text)
    text = re.sub(r"\^", " ^ ", text)
    text = re.sub(r"\+", " + ", text)
    text = re.sub(r"\-", " - ", text)
    text = re.sub(r"\=", " = ", text)
    text = re.sub(r"'", " ", text)
    text = re.sub(r"(\d+)(k)", r"\g<1>000", text)
    text = re.sub(r":", " : ", text)
    text = re.sub(r" e g ", " eg ", text)
    text = re.sub(r" b g ", " bg ", text)
    text = re.sub(r" u s ", " american ", text)
    text = re.sub(r"\0s", "0", text)
    text = re.sub(r" 9 11 ", "911", text)
    text = re.sub(r"e - mail", "email", text)
    text = re.sub(r"j k", "jk", text)
    text = re.sub(r"\s{2,}", " ", text)

    text = text.split()

    return text


def remove_stop_words(words):
    """ Removes all of the stop words.

        Args:
            words: list of string
                The words in the text.
        
        Returns:
            words: list of string
                The words in the text with stop words removed.
    """
    stops = set(stopwords.words("english"))
    return list(filter(lambda w: w not in stops, words))


def setup_layer(max_length, layer_config):
    """ Creates a single Layer for the CNN model.

        Args:
            max_length: int
                The maximum length of the input sequences.
            layer_config: dict
                Contains the information needed to create the layer.

        Returns:
            layer: Layer module
                The desired Layer module.
    """
    blocks = []
    output_sizes = []
    for block_config in layer_config:
        block, output_size = setup_block(max_length, block_config)
        blocks.append(block)
        output_sizes.append(output_size)
    layer = CNNLayer(blocks)
    layer_size = sum(output_sizes)
    return layer, layer_size


def setup_block(max_length, block_config):
    """ Creates a single block for the CNN model.

        Args:
            max_length: int
                The maximum length for each sequence/question.
            block_config: dict
                Contains the information needed to create the block.

        Returns:
            block: Block module
                The desired Block module.
    """
    input_size = block_config["input_size"]
    output_size = block_config["output_size"]
    width = block_config["width"]
    dropout_rate = block_config["dropout_rate"]
    match_score = block_config["match_score"]
    share_weights = block_config["share_weights"]

    if block_config["type"] == "bcnn":
        conv = Convolution(input_size, output_size, width, 1)
        pool = WidthAP(width)
        block = BCNNBlock(conv, pool, dropout_rate=dropout_rate)
    
    elif block_config["type"] == "abcnn1":
        attn = ABCNN1Attention(input_size, max_length, share_weights, match_score)
        conv = Convolution(input_size, output_size, width, 2)
        pool = WidthAP(width)
        block = ABCNN1Block(attn, conv, pool, dropout_rate=dropout_rate)
    
    elif block_config["type"] == "abcnn2":
        conv = Convolution(input_size, output_size, width, 1)
        attn = ABCNN2Attention(max_length, width, match_score)
        block = ABCNN2Block(conv, attn, dropout_rate=dropout_rate)
    
    elif block_config["type"] == "abcnn3":
        attn1 = ABCNN1Attention(input_size, max_length, share_weights, match_score)
        conv = Convolution(input_size, output_size, width, 2)
        attn2 = ABCNN2Attention(max_length, width, match_score)
        block = ABCNN3Block(attn1, conv, attn2, dropout_rate=dropout_rate)

    else:
        raise Exception("Unsupported type. Must be one of 'bcnn', 'abcnn1', 'abcnn2', 'abcnn3'.")

    return block, output_size    


def weights_init(m):
    """ Initializes the weights for the modules in the CNN model. This function 
        is applied recursively to all modules in the model via the "apply"
        function.

        Args:
            m: nn.Module
                The module to initialize.
            
        Returns:
            None
    """ 
    classname = m.__class__.__name__
    if classname.find("Conv2d") != -1:
        nn.init.xavier_normal_(m.weight)
        nn.init.constant_(m.bias, 0)
    elif classname.find("Linear") != -1:
        nn.init.xavier_normal_(m.weight)
        nn.init.constant_(m.bias, 0)
    elif classname.find("ABCNN1Attention") != -1:
        nn.init.xavier_normal_(m.W1)
        nn.init.xavier_normal_(m.W2)
