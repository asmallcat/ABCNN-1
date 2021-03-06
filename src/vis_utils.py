# coding=utf-8

import matplotlib.pyplot as plt

from setup import setup_block

def make_block(model_dict, block_prefix, max_length, block_config):
    """ Creates a block from the model and overwrites its
        weights with the weights from the pre-trained model.

        Args:
            model_dict: dict
                Contains the weights of the pre-trained model.
            block_prefix: string
                Text that appears at the beginning of the relevant
                weights in the pre-trained model.
            max_length: string
                The maximum length of sequences/questions.
            block_config: dict
                The settings to use to create the block.

        Returns:
            block: Block Module
                The initialized block.
    """
    block, output_size = setup_block(max_length, block_config)
    block_dict = block.state_dict()
    for name, weights in model_dict.items():
        if block_prefix in name:
            block_name = name.replace(block_prefix, "")
            if block_name in block_dict:
                block_dict[name] = weights
    block.load_state_dict(block_dict)
    return block, output_size


def plot_attention_matrix(A, row_ticks, col_ticks, filename):
    """ Plots the attention matrix and saves the plot to disk.

        Args:
            A: torch.Tensor of shape (batch_size, 1, max_length, max_length)
                The attention matrix.
            row_ticks: list of string
                The labels to use for the row ticks.
            col_ticks: list of string
                The labels to use for the column ticks.
            filename:
                The name of the output file.

        Returns:
            None
    """
    # Sanity checks
    assert(A.shape[0] == len(row_ticks) and A.shape[1] == len(col_ticks))

    # Make plot bigger so its easier to read
    plt.rcParams["figure.figsize"] = 10, 10

    # Make x-ticks and labels appear on top
    plt.rcParams["xtick.bottom"] = plt.rcParams["xtick.labelbottom"] = False
    plt.rcParams["xtick.top"] = plt.rcParams["xtick.labeltop"] = True

    # Plot the attention distribution
    plt.imshow(A, cmap="rainbow", interpolation="nearest")
    plt.xticks(range(len(col_ticks)), col_ticks, rotation=90)
    plt.yticks(range(len(row_ticks)), row_ticks)
    plt.colorbar()
    plt.savefig(filename)
    plt.clf()
