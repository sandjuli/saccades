"""Synthesize symbolic/toy version of (and solution to) counting problem."""
import sys
import argparse
import numpy as np
import pandas as pd
import torch
from matplotlib import pyplot as plt
from matplotlib.patches import Circle
from matplotlib.collections import PatchCollection
from sklearn.metrics.pairwise import euclidean_distances
from scipy.spatial import distance
from itertools import product, combinations
import random

# EPS = sys.float_info.epsilon
EPS = 1e-7
CHAR_WIDTH = 4
CHAR_HEIGHT = 5

def get_xy_coords(numerosity, noise_level, n_glimpses):
    """Randomly select n spatial locations and take noisy observations of them.

    Nine possible true locations corresponding to a 3x3 grid within a 1x1 space
    locations are : product([0.2, 0.5, 0.8], [0.2, 0.5, 0.8])

    TODO: change how I parameterize noise level to be more intuitive. Make it
    as fraction of distance between objects in the scaled 1x1 space. 0.57
    creates good distribution over integration scores

    Arguments:
        numerosity (int): how many obejcts to place
        noise level (float): how noisy are the observations. nl*0.1/2 = scale. nl*0.1 = cutoff
        n_glimpses: the length of the sequence to construct

    Returns:
        array: x,y coordinates of the glimpses (noisy observations)
        array: symbolic rep (index 0-8) of the glimpsed objects, which true locations
        array: x,y coordinates of the above
    """
    # numerosity = 4
    # noise_level = 1.6
    # nl = 1
    nl = 0.1 * noise_level
    # min_dist = 0.3
    # n_glimpses = 4

    n_symbols = len(POSSIBLE_CENTROIDS)
    # Randomly select where to place objects
    objects = random.sample(range(n_symbols), numerosity)
    object_coords = [POSSIBLE_CENTROIDS[i] for i in objects]

    # Each glimpse is associated with an object idx
    # All objects must be glimpsed, but the glimpse order should be random
    glimpse_candidates = objects.copy()
    while len(glimpse_candidates) < n_glimpses:
        to_append = glimpse_candidates.copy()
        random.shuffle(to_append)
        glimpse_candidates += to_append

    glimpsed_objects = glimpse_candidates[:n_glimpses]
    random.shuffle(glimpsed_objects)
    coords_glimpsed_objects = [POSSIBLE_CENTROIDS[object] for object in glimpsed_objects]

    # Take noisy observations of glimpsed objects
    # vals = [-0.05, .0, 0.05]
    # possible_noise = [(x*nl,y*nl) for (x,y) in product(vals, vals)] + [(x*nl,y*nl) for (x,y) in product([-.1, .1], [.0])] + [(x*nl,y*nl) for (x,y) in product([.0], [-.1, .1])]
    # noises = random.choices(possible_noise, k=n_glimpses)
    noise_x = np.random.normal(loc=0, scale=nl/2, size=n_glimpses)
    noise_y = np.random.normal(loc=0, scale=nl/2, size=n_glimpses)

    while any(abs(noise_x) > nl):
        idx = np.where(abs(noise_x) > nl)[0]
        noise_x[idx] = np.random.normal(loc=0, scale=nl, size=len(idx))
    while any(abs(noise_y) > nl):
        idx = np.where(abs(noise_y) > nl)[0]
        noise_y[idx] = np.random.normal(loc=0, scale=nl, size=len(idx))

    glimpse_coords = [(x+del_x, y+del_y) for ((x,y), (del_x,del_y)) in zip(coords_glimpsed_objects, zip(noise_x, noise_y))]

    return np.array(glimpse_coords), np.array(glimpsed_objects), np.array(coords_glimpsed_objects)


def get_shape_coords(glimpse_coords, objects, noiseless_coords, max_dist,
                     shapes_set, n_shapes, same, distract):
    """Generate glimpse shape feature vectors.

    Each object is randomly assigned one shape. The shape feature
    vector indicates the proximity of each glimpse to objects of each shape.
    If there are no objects of shape m within a radius of max_dist from the
    glimpse, the mth element of the shape vector will be 0. If the glimpse
    coordinates are equal to the coordinates of an object of shape m, the mth
    element of the shape feature vector will be 1.

    Args:
        glimpse_coords (array): xy coordinates of the glimpses
        objects (array): Symbolic representation (0-8) of the object locations
        noiseless_coords (array of tuples): xy coordinates of the above
        max_dist (float): maximum distance to use when bounding proximity measure
        shapes_set (list): The set of shape indexes from which shapes should be sampled
        n_shapes (int): how many shapes exist in this world (including other
                        datasets). Must be greater than the maximum entry in
                        shapes_set.
        same (bool): whether all shapes within one image should be identical
        distract (bool): whether to include distractor shapes

    Returns:
        array: nxn_shapes array where n is number of glimpses.

    """
    #
    seq_len = glimpse_coords.shape[0]
    unique_objects = np.unique(objects)
    unique_object_coords = np.unique(noiseless_coords, axis=0)
    num = len(unique_objects)
    shape_coords = np.zeros((seq_len, n_shapes))
    # Assign a random shape to each object
    if same:  # All shapes in the image will be the same
        shape = np.random.choice(shapes_set)
        shape_assign = np.repeat(shape, num)
    else:
        shape_assign = np.random.choice(shapes_set, size=num, replace=True)
    shape_map = {object: shape for (object, shape) in zip(unique_objects, shape_assign)}
    shape_map_vector = np.ones((GRID_SIZE,)) * -1 # 9 for the 9 locations?
    for object in shape_map.keys():
        shape_map_vector[object] = shape_map[object]
    shape_hist = [sum(shape_map_vector == shape) for shape in range(n_shapes)]
    # Calculate a weighted 'glimpse label' that indicates the shapes in a glimpse
    eu_dist = euclidean_distances(glimpse_coords, unique_object_coords)
    # max_dist = np.sqrt((noise_level*0.1)**2 + (noise_level*0.1)**2)
    glimpse_idx, object_idx = np.where(eu_dist <= max_dist + EPS)
    shape_idx = [shape_map[obj] for obj in unique_objects[object_idx]]
    # proximity = {idx:np.zeros(9,) for idx in glimpse_idx}
    for gl_idx, obj_idx, sh_idx in zip(glimpse_idx, object_idx, shape_idx):
        if max_dist != 0:
            prox = 1 - (eu_dist[gl_idx, obj_idx]/max_dist)
        else:
            prox  = 1
        # print(f'gl:{gl_idx} obj:{obj_idx} sh:{sh_idx}, prox:{prox}')
        shape_coords[gl_idx, sh_idx] += prox
    # shape_coords[glimpse_idx, shape_idx] = 1 - eu_dist[glimpse_idx, obj_idx]/min_dist
    # make sure each glimpse has at least some shape info
    assert np.all(shape_coords.sum(axis=1) > 0)
    return shape_coords, shape_map, shape_hist


class GlimpsedImage():
    def __init__(self, xy_coords, shape_coords, shape_map, shape_hist, objects, max_dist, num_range):
        # self.empty_locations = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9}
        self.empty_locations = set(range(GRID_SIZE))
        self.filled_locations = set()
        self.lower_bound, self.upper_bound = num_range
        self.count = 0

        self.xy_coords = xy_coords
        self.shape_coords = shape_coords
        self.shape_map = shape_map
        self.min_shape = np.argmin(shape_hist)
        self.min_num = shape_hist[self.min_shape]

        self.n_glimpses = xy_coords.shape[0]
        self.objects = objects
        self.max_dist = max_dist
        self.numerosity = len(np.unique(objects))

        # Special cases where the numerosity can be established from the number
        # of unique candidate locations or distinct shapes
        self.special_case_xy = False
        self.shape_count = len(np.unique(shape_coords.nonzero()[1]))
        if self.shape_count == self.upper_bound:
            self.special_case_shape = True
        else:
            self.special_case_shape = False
        self.lower_bound = max(self.lower_bound, self.shape_count)

    def plot_example(self, pass_count, id='000'):
        # assigned_list = [ass for ass in assignment if ass is not None]
        # pred_num = len(assigned_list)
        uni_objs = np.unique(self.objects)
        # unassigned_list = [centroid for centroid in np.arange(9) if centroid not in objects]
        marker_list = ['o', '^', 's', 'p', 'P', '*', 'D', 'X', 'h']
        winter = plt.get_cmap('winter')
        glimpse_colors = winter([0, 0.25, 0.5, 1])
        fig, ax = plt.subplots(figsize=(5, 5))
        # Grid
        for idx in range(9):
            x, y = CENTROID_ARRAY[idx,0], CENTROID_ARRAY[idx, 1]
            plt.scatter(x, y, color='gray', marker=f'${idx}$')
        lim = (-0.0789015869776648, 1.0789015869776648)
        plt.xlim(lim)
        plt.ylim(lim)
        plt.ylabel('Y')
        plt.xlabel('X')
        # plt.savefig('figures/toy_example1.png')
        # plt.axis('equal')
        # ax.spines['top'].set_visible(False)
        # ax.spines['right'].set_visible(False)
        # ax.spines['bottom'].set_visible(False)
        # ax.spines['left'].set_visible(False)

        # Objects
        for obj in uni_objs:
            plt.scatter(CENTROID_ARRAY[obj, 0], CENTROID_ARRAY[obj,1], color='green', marker=marker_list[self.shape_map[obj]], facecolors='none', s=100.0)
        # plt.savefig('figures/toy_example2.png')

        # Glimpses
        xy = self.xy_coords
        for glimpse_idx in range(xy.shape[0]):
            plt.scatter(xy[glimpse_idx,0], xy[glimpse_idx,1], color=glimpse_colors[glimpse_idx], marker=f'${glimpse_idx}$')
        # plt.savefig('figures/toy_example3.png')

        # Noise
        patches = []
        for idx in range(9):
            x, y = CENTROID_ARRAY[idx,0], CENTROID_ARRAY[idx,1]
            patches.append(Circle((x, y), radius=self.max_dist, facecolor='none'))
        p = PatchCollection(patches, alpha=0.1)
        ax.add_collection(p)

        # Assignments
        if pass_count > 0:
            for glimpse_idx, cand_list in enumerate(self.candidates):
                for cand in cand_list:
                    x = [xy[glimpse_idx,0], CENTROID_ARRAY[cand,0]]
                    y = [xy[glimpse_idx,1], CENTROID_ARRAY[cand,1]]
                    if len(cand_list)==1:
                        plt.plot(x, y, color=glimpse_colors[glimpse_idx])
                    else:
                        plt.plot(x, y, '--', color=glimpse_colors[glimpse_idx])
            filled = list(self.filled_locations)
            plt.scatter(CENTROID_ARRAY[filled, 0], CENTROID_ARRAY[filled, 1], marker='o', s=300, facecolors='none', edgecolors='red')
        plt.title(f'count={self.count}')
        # plt.savefig(f'figures/toy_examples/revised_example_{id}.png')

    def process_xy(self):
        """Determine ambiguous glimpses, if any."""
        dist = euclidean_distances(POSSIBLE_CENTROIDS, self.xy_coords)
        dist_th = np.where(dist <= self.max_dist + EPS, dist, -1)
        self.candidates = [np.where(col >= 0)[0] for col in dist_th.T]
        # Count number of unique candidate locations
        unique_cands = set()
        _ = [unique_cands.add(cand) for candlist in self.candidates for cand in candlist]
        if len(unique_cands) == self.lower_bound:
            self.pred_num = self.lower_bound
            self.upper_bound = self.lower_bound
            self.special_case_xy = True
        self.unambiguous_idxs = [idx for idx in range(self.n_glimpses) if len(self.candidates[idx])==1]
        self.ambiguous_idxs = [idx for idx in range(self.n_glimpses) if len(self.candidates[idx])>1]

        for idx in self.unambiguous_idxs:
            loc = self.candidates[idx][0]
            if loc in self.empty_locations:
                self.empty_locations.remove(loc)
                self.filled_locations.add(loc)
        self.count = len(self.filled_locations)
        self.lower_bound = max(self.lower_bound, self.count)

    def check_if_done(self, pass_count):
        """Return True if any of a number of termination conditions are met."""
        # self = example
        if self.lower_bound == self.upper_bound:
            self.pred_num = self.lower_bound
            # print('Done because lower_bound reached upper_bound')
            return True
        if pass_count > 0:
            if not self.ambiguous_idxs:  # no ambiguous glimpses
                self.pred_num = self.count
                # print('Done because no ambiguous glimpses')
                return True
            done = True
            to_be_resolved = dict()
            for j in self.ambiguous_idxs:
                for cand in self.candidates[j]:
                    if cand in self.empty_locations:
                        done = False
                        if j not in to_be_resolved.keys():
                            to_be_resolved[j] = (self.candidates[j], [cand])
                        else:
                            to_be_resolved[j][1].extend([cand])
            self.to_be_resolved = to_be_resolved
            if done:
                # print('Done because no abiguity about numerosity')
                self.pred_num = self.count
        else:
            return False
        return done

    # def use_shape_to_resolve_oner(self, idx, cand_list, new_loc):
    #     # What other assigned glimpses have overlapping candidates?
    #     similar = set()
    #     for ua_idx in self.unambigous_glimpses:
    #         if self.candidates(ua_idx) in cand_list:
    #             similar.add(ua_idx)
    #
    #     ambig_shape_idxs = self.shape_coords[idx, :].nonzero()[1]
    #     similar_shape_idxs = self.shape_coords[list(similar), :].nonzero()[1]
    #     # Does this ambiguous glimpse provide evidence of a shape not associated
    #     # with previously toggled locations?
    #     new_shape = False
    #     for shape_idx in ambig_shape_idxs:
    #         if shape_idx not in similar_shape_idxs:
    #             new_shape = True
    #
    #     # Compare hypotheses
    #     # cand_list is at least two and only one of them is a new location
    #     # options are
    #     # 1) no additional object, there are only the object(s) at a previously
    #     # toggled location
    #     # 2) there is an additional object of the same shape at the new location
    #
    #     if not new_shape:
    #         toggled_cands = [cand for cand in cand_list if cand != new_loc[0]]
    #         #
    #
    #         dist = euclidean_distances(self.xy_coords[idx,:].reshape(1, -1), CENTROID_ARRAY[toggled_cands])
    #         prox = 1 - (dist/self.max_dist)
    #
    #     return new_shape

    def use_shape_to_resolve(self, idx, cand_list):
        """Use shape vector to determine which candidate locations hold objects.
        """
        # idx
        # cand_list
        # self = example
        self.xy_coords.shape
        self.xy_coords[idx,:]
        CENTROID_ARRAY[cand_list]
        dist = euclidean_distances(CENTROID_ARRAY[cand_list], self.xy_coords[idx, :].reshape(1, -1))
        # dist = np.where(dist < self.max_dist, dist, 0)
        prox = (1 - (dist/self.max_dist)).flatten()
        object_locations = []
        for i in range(len(cand_list)):
            if any(np.isclose(self.shape_coords[idx, :], prox[i])):
                object_locations.append(cand_list[i])
        # if any(np.isclose(self.shape_coords[idx, :], prox[1])):
        #     object_locations.append(cand_list[1])
        for comb in combinations(range(len(cand_list)), 2):
            if any(np.isclose(self.shape_coords[idx, :], sum(prox[list(comb)]))):
                object_locations = [cand_list[i] for i in comb]

        return object_locations

    def toggle(self, idx, loc):
        self.candidates[idx] = [loc]
        if loc in self.empty_locations:
            # self.count += 1
            self.empty_locations.remove(loc)
            self.filled_locations.add(loc)
            self.lower_bound = max(self.lower_bound, self.count)
        self.count = len(self.filled_locations)


# def generate_one_example(num, noise_level, pass_count_range, num_range, shapes_set, n_shapes, same):
def generate_one_example(num, config):
    """Synthesize a single sequence and determine numerosity."""
    noise_level = config.noise_level
    min_pass_count = config.min_pass
    max_pass_count = config.max_pass
    num_low = config.min_num
    num_high = config.max_num
    num_range = (num_low, num_high)
    shapes_set = config.shapes
    n_shapes = config.n_shapes
    same = config.same

    # Synthesize glimpses - paired observations of xy and shape coordinates
    max_dist = np.sqrt((noise_level*0.1)**2 + (noise_level*0.1)**2)
    # min_pass_count, max_pass_count = pass_count_range
    # num_low, num_high = num_range
    # num = random.randrange(num_low, num_high + 1)
    final_pass_count = -1
    # This loop will continue synthesizing examples until it finds one within
    # the desired pass count range. The numerosity is determined before hand so
    # that limiting the pass count doesn't bias the distribution of numerosities
    while final_pass_count < min_pass_count or final_pass_count > max_pass_count:
        xy_coords, objects, noiseless_coords = get_xy_coords(num, noise_level, num_high)
        shape_coords, shape_map, shape_hist = get_shape_coords(xy_coords, objects, noiseless_coords, max_dist, shapes_set, n_shapes, same)
        # print(shape_coords)

        # Initialize records
        example = GlimpsedImage(xy_coords, shape_coords, shape_map, shape_hist, objects, max_dist, num_range)
        pass_count = 0
        done = example.check_if_done(pass_count)

        # First pass
        if not done:
            example.process_xy()
            pass_count += 1
            done = example.check_if_done(pass_count)
        initial_candidates = example.candidates.copy()
        initial_filled_locations = example.filled_locations.copy()

        while not done and pass_count < max_pass_count:
            pass_count += 1

            tbr = example.to_be_resolved

            keys = list(tbr.keys())
            idx = keys[0]
            cand_list, loc_list = tbr[idx]
            new_object_idxs = example.use_shape_to_resolve(idx, cand_list)

            for loc in new_object_idxs:
                example.toggle(idx, loc)

            # Check if done
            done = example.check_if_done(pass_count)

        if not done:
            print('DID NOT SOLVE')
            example.pred_num = example.lower_bound
            # example.plot_example(pass_count)

        final_pass_count = pass_count
    unique_objects = set(example.objects)
    filled_locations = [1 if i in unique_objects else 0 for i in range(GRID_SIZE)]
    example_dict = {'xy': xy_coords, 'shape': shape_coords, 'numerosity': num,
                    'predicted num': example.pred_num, 'count': example.count,
                    'locations': filled_locations,
                    'object_coords': noiseless_coords,
                    'shape_map': shape_map,
                    'pass count': pass_count, 'unresolved ambiguity': not done,
                    'special xy': example.special_case_xy,
                    'special shape': example.special_case_shape,
                    'lower bound': example.lower_bound,
                    'upper bound': example.upper_bound,
                    'min shape': example.min_shape, 'min num': example.min_num,
                    'initial candidates': initial_candidates,
                    'initial filled locations': initial_filled_locations,
                    'shape_hist': shape_hist}
    return example_dict


# def generate_dataset(noise_level, n_examples, pass_count_range, num_range, shapes_set, n_shapes, same):
def generate_dataset(config):
    """Fill data frame with toy examples."""
    config = process_args(config)
    # numbers = np.arange(num_range[0], num_range[1] + 1)
    numbers = np.arange(config.min_num, config.max_num + 1)
    n_examples = config.size
    n_repeat = np.ceil(n_examples/len(numbers)).astype(int)
    nums = np.tile(numbers, n_repeat)
    # data = [generate_one_example(nums[i], noise_level, pass_count_range, num_range, shapes_set, n_shapes, same) for i in range(n_examples)]
    data = [generate_one_example(nums[i], config) for i in range(n_examples)]
    df = pd.DataFrame(data)
    # df['pass count'].hist()
    # df[df['unresolved ambiguity'] == True]
    # df[df['numerosity'] != df['predicted num']]
    # df['pass count'].max()
    return df

def process_args(conf):
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

    if conf.shapes[0].isnumeric():
        conf.shapes = [int(i) for i in conf.shapes]
    elif conf.shapes[0].isalpha():
        letter_map = {'A':0, 'B':1, 'C':2, 'D':3, 'E':4, 'F':5, 'G':6, 'H':7,
                      'J':8, 'K':9, 'N':10, 'O':11, 'P':12, 'R':13, 'S':14,
                      'U':15, 'Z':16}
        conf.shapes = [letter_map[i] for i in conf.shapes]
    return conf

def main():
    parser = argparse.ArgumentParser(description='PyTorch network settings')
    parser.add_argument('--min_pass', type=int, default=0)
    parser.add_argument('--max_pass', type=int, default=6)
    parser.add_argument('--min_num', type=int, default=2)
    parser.add_argument('--max_num', type=int, default=7)
    parser.add_argument('--shapes', type=list, default=[0, 1, 2, 3, 5, 6, 7, 8])
    parser.add_argument('--noise_level', type=float, default=1.6)
    parser.add_argument('--size', type=int, default=100)
    parser.add_argument('--n_shapes', type=int, default=10, help='How many shapes to the relevant training and test sets span?')
    parser.add_argument('--same', action='store_true', default=False)
    parser.add_argument('--grid', type=int, default=9)
    conf = parser.parse_args()

    same = 'same' if conf.same else ''
    shapes = ''.join(conf.shapes)
    # adding gw6 to this file name even those there are no glimpses yet just to save trouble
    fname = f'toysets/toy_dataset_num{conf.min_num}-{conf.max_num}_nl-{conf.noise_level}_diff{conf.min_pass}-{conf.max_pass}_{shapes}{same}_grid{conf.grid}_{conf.size}.pkl'
    data = generate_dataset(conf)
    data.to_pickle(fname)

if __name__ == '__main__':
    main()
