"""Add pseudo images containing numeric characters to toy dataset."""
import os
import argparse
import numpy as np
import pandas as pd
from itertools import product
from matplotlib import pyplot as plt
from scipy.io import savemat
from scipy.stats import multivariate_normal
from skimage.transform import warp_polar

import toy_model_data as toy
from letters import get_alphabet
CHAR_WIDTH = 4
CHAR_HEIGHT = 5
# GRID = [0.2, 0.5, 0.8]
# GRID = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
# NCOLS = len(GRID)
# PIXEL_HEIGHT = (CHAR_HEIGHT + 2) * NCOLS
# PIXEL_WIDTH = (CHAR_WIDTH + 2) * NCOLS
# PIXEL_X = [col*(CHAR_WIDTH + 2) + 1 for col in range(NCOLS)]   #[1, 6, 11] # col *5 + 1
# PIXEL_Y = [row*(CHAR_HEIGHT + 2) + 1 for row in range(NCOLS)]
# POSSIBLE_CENTROIDS = [(x, y) for (x, y) in product(GRID, GRID)]
# PIXEL_TOPLEFT = [(x, y) for (x, y) in product(PIXEL_X, PIXEL_Y)]
# MAP_SCALE_PIXEL = {scaled:pixel for scaled,pixel in zip(POSSIBLE_CENTROIDS, PIXEL_TOPLEFT)}
# CENTROID_ARRAY = np.array(POSSIBLE_CENTROIDS)


shape_holder = np.zeros((5, 4))
chars = get_alphabet()
pixel_counts = [np.sum(char) for char in chars]
# np.mean(pixel_counts[0:5])
# np.mean(pixel_counts[5:10])
# for char in chars:
#     # plt.matshow(char)
#     print(np.sum(char))
# test_luminances = [0.2, 0.4, 0.6, 0.8, 1]
# train_luminances = [0.1, 0.3, 0.5, 0.7, 0.9]
test_luminances = [0.1, 0.3, 0.7, 0.9]
train_luminances = [0, 0.5, 1]

def get_solarized(image, fg_lum, bg_lum):
    """Solarize image.

    Args:
        image (array): image template of just zeros and ones
        fg_lum (float): foregound luminance value
        bg_lum (float): backgound luminance value

    Returns:
        array: solarized image. zeros mapped to bg lum, ones mapped to fg lum
    """
    dict = {0: bg_lum, 1: fg_lum}
    solarized = np.vectorize(dict.get)(image)
    return solarized

def get_solarized_noise(image, fg_lum, bg_lum):
    """Sample bg and fg luminances from distributions.

    Args:
        image (array): image template of just zeros and ones
        fg_lum (float): foregound luminance value
        bg_lum (float): backgound luminance value

    Returns:
        array: solarized image. zeros mapped to bg lum, ones mapped to fg lum
    """

    dict = {0: bg_lum, 1: fg_lum}
    # dict = {0: np.random.normal(loc=bg_lum, scale=0.1), 1: np.random.normal(loc=fg_lum, scale=0.1)}
    solarized = np.vectorize(dict.get)(image)
    noised = np.vectorize(np.random.normal)(loc=solarized, scale=0.05)
    noised = np.float32(noised)
    return noised

def get_overlap(char_set):
    overlaps = []
    for i, char1 in enumerate(char_set):
        for j, char2 in enumerate(char_set[1:]):
            if j < i:
                continue
            overlaps.append(np.sum(char1 == char2))
    avg = np.mean(overlaps)
    return avg


def add_char_glimpses(data, conf):
    """Synthesize and glimpse small image of pixel corners.

    The image is a 12 by 12 grid (+ a 2 pixel border). The 9 possible object
    locations from the toy dataset generator thus correspond to 4x4 regions
    of the image. Here my shapes are the 4 possible 3-pixel corners:
    1 0     0 1     1 1     1 1
    1 1     1 1     1 0     0 1

    The number and location of these shapes as well as the location of the
    the glimpses of these shapes are determined by the toy_model_data
    generator used for previous experiments. Thus we retain the knowledge of
    the difficulty of each 'image', or how much it requires the integration of
    both sources of glimpse information (glimpse content and glimpse location,
    what and where) to determine the numerosity.

    The synthesized image as well as the glimpse sequences are appended as new
    columns to the existing data frame. These new columns can then be used as
    input instead of the existing xy and shape column, used in previous
    experiments.
    """
    # data = pd.read_pickle('toysets/toy_dataset_num2-6_nl-0.6_diff0-6_[0, 1, 2]_21.pkl')
    # lums = test_luminances if len(data) < 10000 else train_luminances
    # i=0
    glim_wid = conf.glimpse_wid
    solarize = conf.solarize
    lums = conf.luminances
    glimpse = not conf.no_glimpse
    noise_level = 0.1 * conf.noise_level
    half_glim = glim_wid//2
    border = half_glim
    # Initialize new columns to be filled
    data['glimpse_coords'] = None
    # data['bw image'] = None
    # data['solarized image'] = None
    data['luminances'] = None
    data['noised_image'] = None
    data['saliency'] = None
    if glimpse:
        # data['bw glimpse pixels'] = None
        # data['sol glimpse pixels'] = None
        data['noi_glimpse_pixels'] = None
    data['char overlap'] = None
   
    # i=0
    for i in range(len(data)):
        if not i % 10:
            print(f'Synthesizing image {i}', end='\r')
        row = data.iloc[i]
        # Coordinates in 1x1 space
        object_xy_coords = CENTROID_ARRAY[np.where(row.locations)[0]]
        # Calculate saliency map
        # Saliency map should be the size of the image WITHOUT the added border
        image_size = (PIXEL_HEIGHT, PIXEL_WIDTH)
        saliency = get_saliency(object_xy_coords, noise_level, image_size)
        data.at[i, 'saliency'] = saliency
        object_pixel_coords = [MAP_SCALE_PIXEL[tuple(xy)] for xy in object_xy_coords]
        # Shape indices for the objects
        object_shapes = [row['shape_map'][obj] for obj in np.where(row.locations)[0]]
        char_set = [chars[idx] for idx in object_shapes]

        char_similarity = get_overlap(char_set)
        data.at[i, 'char_overlap'] = char_similarity

        # Insert the specified shapes into the image at the specified locations
        image = np.zeros(image_size)
        # plt.matshow(image, origin='lower')
        # plt.plot([3.5, 3.5], [0, 11], color='cyan')
        # plt.plot([7.5, 7.5], [0, 11], color='cyan')
        # plt.plot([0, 11], [3.5, 3.5], color='cyan')
        # plt.plot([0, 11], [7.5, 7.5], color='cyan')
        for shape_idx, (x,y) in zip(object_shapes, object_pixel_coords):
                # print(f'{shape_idx} {x} {y}')
                # yy = pixel_height - y - 5
                # xx = pixel_width - x - 3
                # image[yy:yy+char_height:, xx:xx+char_width] = chars[shape_idx]
                image[y:y+CHAR_HEIGHT:, x:x+CHAR_WIDTH] = chars[shape_idx]

        # Convert glimpse coordinates to pixel coordinates
        scaled_glimpse_coords = row.xy.copy()
        scaled_glimpse_coords[:, 0] = scaled_glimpse_coords[:, 0]*PIXEL_WIDTH
        scaled_glimpse_coords[:, 1] = scaled_glimpse_coords[:, 1]*PIXEL_HEIGHT
        glimpse_coords = np.round(scaled_glimpse_coords).astype(int)

        # plt.matshow(image, origin='upper')
        # plt.scatter(glimpse_coords[:,0], glimpse_coords[:,1], color='red')
        # plt.scatter(scaled_glimpse_coords[:,0], scaled_glimpse_coords[:,1], color='green')

        # Add border of half_glim pixels so all gimpses are the same size
        # glim_wid = 6

        image_wbord = np.zeros((PIXEL_HEIGHT+glim_wid, PIXEL_WIDTH+glim_wid))
        image_wbord[half_glim:-half_glim,half_glim:-half_glim] = image
        # data.at[i, 'bw image'] = image_wbord
        glimpse_coords += half_glim
        # if save preglimpsed
        if glimpse > 0:
            glimpse_pixels = [image_wbord[y-half_glim:y+half_glim, x-half_glim:x+half_glim].flatten() for x,y in glimpse_coords]
            # data.at[i, 'bw glimpse pixels'] = glimpse_pixels
        # plt.matshow(image_wbord, origin='upper')
        

        # Optionaly solarize the image
        if solarize:
            fg, bg = np.random.choice(lums, size=2, replace=False)
            data.at[i, 'luminances'] = [fg, bg]
            # ensure that the difference between the foreground and background
            # is at least 0.2, which is the smallest difference in the test sets
            while abs(fg - bg) < 0.2:
                fg, bg = np.random.choice(lums, size=2, replace=False)
            solarized = get_solarized(image_wbord, fg, bg)
            # data.at[i, 'solarized image'] = solarized
            
            noised = get_solarized_noise(image_wbord, fg, bg)
            data.at[i, 'noised_image'] = noised
            # if save preglimpsed
            if glimpse:
                glimpse_pixels_sol = [solarized[y-half_glim:y+half_glim, x-half_glim:x+half_glim].flatten() for x,y in glimpse_coords]
                # data.at[i, 'sol glimpse pixels'] = glimpse_pixels_sol
                glimpse_pixels_noi = [noised[y-half_glim:y+half_glim, x-half_glim:x+half_glim].flatten() for x,y in glimpse_coords]
                data.at[i, 'noi_glimpse_pixels'] = glimpse_pixels_noi

        # Extract glimpse pixels
        # glimpse_pixels[0].shape
        # Store glimpse data and image in the original dataframe
        data.at[i, 'glimpse_coords'] = glimpse_coords

        # Plotting
        # glimpse_pixels_to_plot = [image_wbord[y-half_glim:y+half_glim, x-half_glim:x+half_glim]for x,y in glimpse_coords]
        # bounding = [plt.Rectangle((x-half_glim-0.5, y-half_glim-0.5), glim_wid, glim_wid, fc='none',ec="red") for x,y in glimpse_coords]
        # plt.matshow(glimpse_pixels_to_plot[0], origin='upper')
        # plt.matshow(glimpse_pixels_to_plot[1], origin='upper')
        # plt.matshow(glimpse_pixels_to_plot[2], origin='upper')
        # plt.matshow(glimpse_pixels_to_plot[3], origin='upper')
        # fig, ax = plt.subplot()
        # plt.matshow(image_wbord, origin='upper')
        # plt.scatter(glimpse_coords[:,0]-0.5, glimpse_coords[:,1]-0.5, color='red')
        # for box in bounding:
        #     plt.gca().add_patch(box)
        #
    return data


def add_logpolar_glimpses(data, conf):
    glim_wid = conf.glimpse_wid
    lums = conf.luminances
    half_glim = glim_wid//2
    data['glimpse_coords'] = None
    data['luminances'] = None
    data['noised_image'] = None
    data['centre_fixation'] = None
    data['logpolar_pixels'] = None
    for i in range(len(data)):
        if not i % 10:
            print(f'Synthesizing image {i}', end='\r')
        row = data.iloc[i]
        # Coordinates in 1x1 space
        object_xy_coords = CENTROID_ARRAY[np.where(row.locations)[0]]
        # Calculate saliency map
        # Saliency map should be the size of the image WITHOUT the added border
        image_size = (PIXEL_HEIGHT, PIXEL_WIDTH)
        object_pixel_coords = [MAP_SCALE_PIXEL[tuple(xy)] for xy in object_xy_coords]
        # Shape indices for the objects
        object_shapes = [row['shape_map'][obj] for obj in np.where(row.locations)[0]]

        # Insert the specified shapes into the image at the specified locations
        image = np.zeros(image_size, dtype=np.float32)
        for shape_idx, (x,y) in zip(object_shapes, object_pixel_coords):
                image[y:y+CHAR_HEIGHT:, x:x+CHAR_WIDTH] = chars[shape_idx]

        # Convert scaled glimpse coordinates to pixel coordinates
        scaled_glimpse_coords = row.xy.copy()
        scaled_glimpse_coords[:, 0] = scaled_glimpse_coords[:, 0]*PIXEL_WIDTH
        scaled_glimpse_coords[:, 1] = scaled_glimpse_coords[:, 1]*PIXEL_HEIGHT
        glimpse_coords = np.round(scaled_glimpse_coords).astype(int)
        data.at[i, 'glimpse_coords'] = glimpse_coords
        imsize = [PIXEL_HEIGHT+glim_wid, PIXEL_WIDTH+glim_wid]
        image_wbord = np.zeros(imsize, dtype=np.float32)
        image_wbord[half_glim:-half_glim,half_glim:-half_glim] = image
        # data.at[i, 'bw image'] = image_wbord
        glimpse_coords += half_glim
        
        # Solarize and add Gaussian noise
        fg, bg = np.random.choice(lums, size=2, replace=False)
        # ensure that the difference between the foreground and background
        # is at least 0.2, which is the smallest difference in the test sets
        while abs(fg - bg) < 0.2:
            fg, bg = np.random.choice(lums, size=2, replace=False)
        data.at[i, 'luminances'] = [fg, bg]
        noised = get_solarized_noise(image_wbord, fg, bg)
        data.at[i, 'noised_image'] = noised

        # Warp noised image to generaye log-polar glimpses
        # Fixed gaze at the centre
        centre = [size//2 for size in imsize]
        fixation = warp_polar(noised, scaling='log', output_shape=imsize, center=centre, mode='edge')  # rows, cols
        data.at[i, 'centre_fixation'] = fixation
        # Glimpses
        lp_glimpses = [warp_polar(noised, scaling='log', output_shape=imsize, center=(y, x), mode='edge') for x, y in glimpse_coords]
        data.at[i, 'logpolar_pixels'] = lp_glimpses
    return data


def get_saliency(object_xy_coords, noise_level, image_size):
    cov = [[noise_level/2, 0], [0, noise_level/2]]
    ny, nx = image_size
    x = np.linspace(0, 1, nx)
    y = np.linspace(0, 1, ny)
    xv, yv = np.meshgrid(x, y)
    pos = np.dstack((xv, yv))
    smap = np.zeros(image_size)
    for loc in object_xy_coords:
        salience = multivariate_normal(loc, cov)
        smap += salience.pdf(pos)
    return smap


def add_char_glimpses_2channel(data, glim_wid=6, solarize=True, lums=[0, 1, 0.5]):
    """Synthesize and glimpse small image of pixel corners.

    The image is a 12 by 12 grid (+ a 2 pixel border). The 9 possible object
    locations from the toy dataset generator thus correspond to 4x4 regions
    of the image. Here my shapes are the 4 possible 3-pixel corners:
    1 0     0 1     1 1     1 1
    1 1     1 1     1 0     0 1

    The number and location of these shapes as well as the location of the
    the glimpses of these shapes are determined by the toy_model_data
    generator used for previous experiments. Thus we retain the knowledge of
    the difficulty of each 'image', or how much it requires the integration of
    both sources of glimpse information (glimpse content and glimpse location,
    what and where) to determine the numerosity.

    The synthesized image as well as the glimpse sequences are appended as new
    columns to the existing data frame. These new columns can then be used as
    input instead of the existing xy and shape column, used in previous
    experiments.
    """
    # data = pd.read_pickle('toysets/toy_dataset_num2-6_nl-0.6_diff0-6_[0, 1, 2]_21.pkl')
    # lums = test_luminances if len(data) < 10000 else train_luminances
    # i=0
    data['glimpse coords'] = None
    data['bw image'] = None
    data['solarized image'] = None
    data['noised image'] = None
    data['dist noised image'] = None
    data['target noised image'] = None
    data['bw glimpse pixels'] = None
    data['sol glimpse pixels'] = None
    data['noi glimpse pixels'] = None
    data['dist noi glimpse pixels'] = None
    data['target noi glimpse pixels'] = None
    data['char overlap'] = None
    # i=0
    for i in range(len(data)):
        if not i % 10:
            print(f'Synthesizing image {i}', end='\r')
        row = data.iloc[i]
        # Coordinates in 1x1 space
        object_xy_coords = CENTROID_ARRAY[np.where(row.locations)[0]]
        object_pixel_coords = [MAP_SCALE_PIXEL[tuple(xy)] for xy in object_xy_coords]
        # Shape indices for the objects
        object_shapes = [row['shape_map'][obj] for obj in np.where(row.locations)[0]]
        char_set = [chars[idx] for idx in object_shapes]

        char_similarity = get_overlap(char_set)
        data.at[i, 'char overlap'] = char_similarity

        # Insert the specified shapes into the image at the specified locations
        image = np.zeros((PIXEL_HEIGHT, PIXEL_WIDTH))
        dist_image = np.zeros((PIXEL_HEIGHT, PIXEL_WIDTH))
        target_image = np.zeros((PIXEL_HEIGHT, PIXEL_WIDTH))
        # plt.matshow(image, origin='lower')
        # plt.plot([3.5, 3.5], [0, 11], color='cyan')
        # plt.plot([7.5, 7.5], [0, 11], color='cyan')
        # plt.plot([0, 11], [3.5, 3.5], color='cyan')
        # plt.plot([0, 11], [7.5, 7.5], color='cyan')

        for shape_idx, (x,y) in zip(object_shapes, object_pixel_coords):
                # print(f'{shape_idx} {x} {y}')
                # yy = pixel_height - y - 5
                # xx = pixel_width - x - 3
                # image[yy:yy+char_height:, xx:xx+char_width] = chars[shape_idx]
                # Add distractors to one channel, everything else to the other
                image[y:y+CHAR_HEIGHT:, x:x+CHAR_WIDTH] = chars[shape_idx]
                if shape_idx == 0:
                    dist_image[y:y+CHAR_HEIGHT:, x:x+CHAR_WIDTH] = chars[shape_idx]
                else:
                    target_image[y:y+CHAR_HEIGHT:, x:x+CHAR_WIDTH] = chars[shape_idx]

        # Convert glimpse coordinates to pixel coordinates
        scaled_glimpse_coords = row.xy.copy()
        scaled_glimpse_coords[:, 0] = scaled_glimpse_coords[:, 0]*PIXEL_WIDTH
        scaled_glimpse_coords[:, 1] = scaled_glimpse_coords[:, 1]*PIXEL_HEIGHT
        glimpse_coords = np.round(scaled_glimpse_coords).astype(int)

        # plt.matshow(image, origin='upper')
        # plt.scatter(glimpse_coords[:,0], glimpse_coords[:,1], color='red')
        # plt.scatter(scaled_glimpse_coords[:,0], scaled_glimpse_coords[:,1], color='green')

        # Add border of half_glim pixels so all gimpses are the same size
        # glim_wid = 6
        half_glim = glim_wid//2
        border = half_glim
        image_wbord = np.zeros((PIXEL_HEIGHT+glim_wid, PIXEL_WIDTH+glim_wid))
        dist_image_wbord = np.zeros((PIXEL_HEIGHT+glim_wid, PIXEL_WIDTH+glim_wid))
        target_image_wbord = np.zeros((PIXEL_HEIGHT+glim_wid, PIXEL_WIDTH+glim_wid))
        image_wbord[half_glim:-half_glim,half_glim:-half_glim] = image
        dist_image_wbord[half_glim:-half_glim,half_glim:-half_glim] = dist_image
        target_image_wbord[half_glim:-half_glim,half_glim:-half_glim] = target_image
        glimpse_pixels = [image_wbord[y-half_glim:y+half_glim, x-half_glim:x+half_glim].flatten() for x,y in glimpse_coords]
        # dist_glimpse_pixels = [dist_image_wbord[y-half_glim:y+half_glim, x-half_glim:x+half_glim].flatten() for x,y in glimpse_coords]

        data.at[i, 'bw image'] = image_wbord
        data.at[i, 'bw glimpse pixels'] = glimpse_pixels
        # plt.matshow(image_wbord, origin='upper')
        glimpse_coords += half_glim

        # Optionaly solarize the image
        if solarize:
            fg, bg = np.random.choice(lums, size=2, replace=False)
            # ensure that the difference between the foreground and background
            # is at least 0.2, which is the smallest difference in the test sets
            while abs(fg - bg) < 0.2:
                fg, bg = np.random.choice(lums, size=2, replace=False)
            solarized = get_solarized(image_wbord, fg, bg)
            # dist_solarized = get_solarized(dist_image_wbord, fg, bg)
            glimpse_pixels_sol = [solarized[y-half_glim:y+half_glim, x-half_glim:x+half_glim].flatten() for x,y in glimpse_coords]
            # dist_glimpse_pixels_sol = [dist_solarized[y-half_glim:y+half_glim, x-half_glim:x+half_glim].flatten() for x,y in glimpse_coords]

            data.at[i, 'solarized image'] = solarized
            data.at[i, 'sol glimpse pixels'] = glimpse_pixels_sol
            noised = get_solarized_noise(image_wbord, fg, bg)
            dist_noised = get_solarized_noise(dist_image_wbord, fg, bg)
            target_noised = get_solarized_noise(target_image_wbord, fg, bg)
            glimpse_pixels_noi = [noised[y-half_glim:y+half_glim, x-half_glim:x+half_glim].flatten() for x,y in glimpse_coords]
            dist_glimpse_pixels_noi = [dist_noised[y-half_glim:y+half_glim, x-half_glim:x+half_glim].flatten() for x,y in glimpse_coords]
            target_glimpse_pixels_noi = [target_noised[y-half_glim:y+half_glim, x-half_glim:x+half_glim].flatten() for x,y in glimpse_coords]

            data.at[i, 'noised image'] = noised
            data.at[i, 'dist noised image'] = dist_noised
            data.at[i, 'target noised image'] = dist_noised
            data.at[i, 'noi glimpse pixels'] = glimpse_pixels_noi
            data.at[i, 'dist noi glimpse pixels'] = dist_glimpse_pixels_noi
            data.at[i, 'target noi glimpse pixels'] = target_glimpse_pixels_noi

        # Extract glimpse pixels
        # glimpse_pixels[0].shape
        # Store glimpse data and image in the original dataframe
        data.at[i, 'glimpse coords'] = glimpse_coords

        # Plotting
        # glimpse_pixels_to_plot = [image_wbord[y-half_glim:y+half_glim, x-half_glim:x+half_glim]for x,y in glimpse_coords]
        # bounding = [plt.Rectangle((x-half_glim-0.5, y-half_glim-0.5), glim_wid, glim_wid, fc='none',ec="red") for x,y in glimpse_coords]
        # plt.matshow(glimpse_pixels_to_plot[0], origin='upper')
        # plt.matshow(glimpse_pixels_to_plot[1], origin='upper')
        # plt.matshow(glimpse_pixels_to_plot[2], origin='upper')
        # plt.matshow(glimpse_pixels_to_plot[3], origin='upper')
        # fig, ax = plt.subplot()
        # plt.matshow(image_wbord, origin='upper')
        # plt.scatter(glimpse_coords[:,0]-0.5, glimpse_coords[:,1]-0.5, color='red')
        # for box in bounding:
        #     plt.gca().add_patch(box)
        #
    return data


def add_chars(fname):
    if os.path.exists(fname):
        print(f'Loading saved dataset {fname}')
        data = pd.read_pickle(fname)
    else:
        print('Dataset does not exist')
        exit()
    # data = add_char_glimpses(data)
    data = add_char_glimpses_2channel(data)

    print(f'Saving {fname} with numeric character images')
    data.to_pickle(fname)
    return data


def define_globals(conf):
    """Set global variables and convert strings to ints."""
    global GRID, POSSIBLE_CENTROIDS, GRID_SIZE, CENTROID_ARRAY
    global PIXEL_HEIGHT, PIXEL_WIDTH, MAP_SCALE_PIXEL

    if conf.grid == 3:
        GRID = [0.2, 0.5, 0.8]
    elif conf.grid == 9:
        GRID = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
    else:
        # print('Grid size not implemented')
        GRID = np.linspace(0.1, 0.9, conf.grid)
    NCOLS = len(GRID)
    PIXEL_HEIGHT = (CHAR_HEIGHT + 2) * NCOLS
    PIXEL_WIDTH = (CHAR_WIDTH + 2) * NCOLS
    PIXEL_X = [col*(CHAR_WIDTH + 2) + 1 for col in range(NCOLS)]   #[1, 6, 11] # col *5 + 1
    PIXEL_Y = [row*(CHAR_HEIGHT + 2) + 1 for row in range(NCOLS)]
    PIXEL_TOPLEFT = [(x, y) for (x, y) in product(PIXEL_X, PIXEL_Y)]
    POSSIBLE_CENTROIDS = [(x, y) for (x, y) in product(GRID, GRID)]
    MAP_SCALE_PIXEL = {scaled:pixel for scaled,pixel in zip(POSSIBLE_CENTROIDS, PIXEL_TOPLEFT)}
    GRID_SIZE = len(POSSIBLE_CENTROIDS)
    CENTROID_ARRAY = np.array(POSSIBLE_CENTROIDS)

def process_args(conf):
    if isinstance(conf.shapes[0], str):
        if conf.shapes[0].isnumeric():
            conf.shapes = [int(i) for i in conf.shapes]
        elif conf.shapes[0].isalpha():
            letter_map = {'A':0, 'B':1, 'C':2, 'D':3, 'E':4, 'F':5, 'G':6, 'H':7, 'I':8,
                          'J':9, 'K':10, 'N':11, 'O':12, 'P':13, 'R':14, 'S':15,
                          'U':16, 'Z':17}
            conf.shapes = [letter_map[i] for i in conf.shapes]
    return conf


def main():
    parser = argparse.ArgumentParser(description='PyTorch network settings')
    parser.add_argument('--min_pass', type=int, default=0)
    parser.add_argument('--max_pass', type=int, default=13)
    parser.add_argument('--min_num', type=int, default=2)
    parser.add_argument('--max_num', type=int, default=7)
    parser.add_argument('--shapes', type=list, default=[0, 1, 2, 3, 5, 6, 7, 8], help='string of numerals 0123 or letters ABCD')
    parser.add_argument('--luminances', nargs='*', type=float, default=[0, 0.5, 1], help='at least two values between 0 and 1')
    parser.add_argument('--noise_level', type=float, default=1.7)
    parser.add_argument('--size', type=int, default=100)
    parser.add_argument('--n_shapes', type=int, default=10, help='How many shapes to the relevant training and test sets span?')
    parser.add_argument('--glimpse_wid', type=int, default=6, help='How many pixels wide and tall should glimpses be?')
    parser.add_argument('--same', action='store_true', default=False)
    parser.add_argument('--solarize', action='store_true', default=False)
    parser.add_argument('--grid', type=int, default=9)
    parser.add_argument('--challenge', type=str, default=None)
    parser.add_argument('--no_glimpse', action='store_true', default=False)
    parser.add_argument('--logpolar', action='store_true', default=False)
    # parser.add_argument('--distract', action='store_true', default=False)
    # parser.add_argument('--distract_corner', action='store_true', default=False)
    # parser.add_argument('--random', action='store_true', default=False)
    parser.add_argument('--n_glimpses', type=int, default=None, help='how many glimpses to generate per image')
    parser.add_argument('--policy', type=str, default='cheat+jitter')
    conf = parser.parse_args()

    same = 'same' if conf.same else ''
    challenge = f'_{conf.challenge}' if conf.challenge is not None else ''
    # solar = 'solarized_' if conf.solarize else ''
    transform = 'logpolar_' if conf.logpolar else f'gw{conf.glimpse_wid}_'
    shapes = ''.join(conf.shapes)
    if conf.no_glimpse:
        n_glimpses = 'nogl'
    elif conf.n_glimpses is not None:
        n_glimpses = f'{conf.n_glimpses}_'
    else:
        n_glimpses = ''
    policy = conf.policy
    # fname_gw = f'toysets/toy_dataset_num{conf.min_num}-{conf.max_num}_nl-{conf.noise_level}_diff{conf.min_pass}-{conf.max_pass}_{shapes}{same}{challenge}_grid{conf.grid}_lum{conf.luminances}_gw{conf.glimpse_wid}_{solar}{n_glimpses}{conf.size}.pkl'
    # fname = f'toysets/toy_dataset_num{conf.min_num}-{conf.max_num}_nl-{conf.noise_level}_diff{conf.min_pass}-{conf.max_pass}_{shapes}{same}{challenge}_grid{conf.grid}_{n_glimpses}{conf.size}.pkl'
    # base = f'num{conf.min_num}-{conf.max_num}_nl-{conf.noise_level}_{shapes}{same}{challenge}_grid{conf.grid}_policy-{policy}_{n_glimpses}{conf.size}'
    fname_gw = f'toysets/num{conf.min_num}-{conf.max_num}_nl-{conf.noise_level}_{shapes}{same}{challenge}_grid{conf.grid}_policy-{policy}_lum{conf.luminances}_{transform}{n_glimpses}{conf.size}'
    # fname = f'toysets/{base}.pkl'
    if not os.path.exists(fname_gw):
        os.mkdir(fname_gw)
    
    
    # if os.path.exists(fname):
    #     print(f'Loading saved dataset {fname}')
    #     data = pd.read_pickle(fname)
    # else:
    print('Generating new dataset')
    define_globals(conf)
    conf = process_args(conf)  
    data = toy.generate_dataset(conf)  # Generate toy version, apply symbolic model
    if conf.logpolar:
        data = add_logpolar_glimpses(data, conf)
    else:
        data = add_char_glimpses(data, conf)  # Add image/pixels/glimpse contents
    
    # Save example images for viewing
    sample = np.random.choice(np.arange(len(data)), 10)

    for idx in sample:
        plt.matshow(data.iloc[idx]['noised_image'], vmin=0, vmax=1, cmap='Greys')
        plt.axis('off')
        plt.savefig(f'{fname_gw}/example_{idx}.png', bbox_inches='tight', dpi=300)    
    
    # data = add_char_glimpses_2channel(data, conf.glimpse_wid, conf.solarize, conf.luminances)
    # fname = f'toysets/toy_dataset_nl-{noise_level}_diff-{min_pass_count}-{max_pass_count}_{shapes_set}_{size}_tetris.pkl'
    print(f'Saving {fname_gw}.pkl')
    data.to_pickle(fname_gw + '.pkl')
    # dict_for_mat = {name: col.values for name, col in data.items()}
    # savemat(f'toysets/stimuli/grid9.mat', dict_for_mat)

if __name__ == '__main__':
    main()
