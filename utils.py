"""Module containing utility functions.
"""

# Standard libraries
import os
import errno
from math import log10, floor
from argparse import ArgumentParser

# Local imports
import CUDAParams

line_start = '  '
comment = dict(c='//', cuda='//',
                fortran='!', matlab='%')

# list of supported languages
langs = ['c', 'cuda', 'fortran', 'matlab']

# source code file extensions based on language
file_ext = dict(c='.c', cuda='.cu',
                fortran='.f90', matlab='.m'
                )

# header extensions based on language
header_ext = dict(c='.h', cuda='.cuh')

# line endings dependent on language
line_end = dict(c=';\n', cuda=';\n',
                fortran='\n', matlab=';\n'
                )

# exp10 functions for various languages
exp_10_fun = dict(c="pow(10.0, ", cuda='exp10(',
                  fortran='exp(log(10) * ', matlab='exp(log(10.0) * '
                  )

# the characters to format an index into an array per language
array_chars = dict(c="[{}]", cuda="[{}]",
                   fortran="({})", matlab="({})"
                   )

def get_species_mappings(num_specs, last_species):
    """
    Consolidate the forward/backwards species mapping
    in one place.  Only to be used 
    """

    fwd_species_map = range(num_specs)
    back_species_map = range(num_specs)

    #in the forward mapping process
    #last_species -> end
    #all entries after last_species are reduced by one
    back_species_map[last_species + 1:] = back_species_map[last_species:-1]
    back_species_map[last_species] = num_specs - 1

    #in the backwards mapping
    #end -> last_species
    #all entries with value >= last_species are increased by one
    ind = fwd_species_map.index(last_species)
    fwd_species_map[ind:-1] = fwd_species_map[ind + 1:]
    fwd_species_map[-1] = last_species

    return fwd_species_map, back_species_map



def get_nu(isp, rxn):
    """Returns the net nu of species isp for the reaction rxn
    """
    if isp in rxn.prod and isp in rxn.reac:
        nu = (rxn.prod_nu[rxn.prod.index(isp)] -
              rxn.reac_nu[rxn.reac.index(isp)])
        # check if net production zero
        if nu == 0:
            return 0
    elif isp in rxn.prod:
        nu = rxn.prod_nu[rxn.prod.index(isp)]
    elif isp in rxn.reac:
        nu = -rxn.reac_nu[rxn.reac.index(isp)]
    else:
        # doesn't participate in reaction
        return 0
    return nu


def read_str_num(string, sep=None):
    """Returns a list of floats pulled from a string.

    Delimiter is optional; if not specified, uses whitespace.

    Parameters
    ----------
    string : str
        String to be parsed.
    sep : str, optional
        Delimiter (default is None, which means consecutive whitespace).

    Returns
    -------
    list of float
        Floats separated by `sep` in `string`.

    """

    # separate string into space-delimited strings of numbers
    num_str = string.split(sep)
    return [float(n) for n in num_str]


def split_str(seq, length):
    """Separate a string seq into length-sized pieces.

    Parameters
    ----------
    seq : str
        String containing sequence of smaller strings of constant length.
    length : int
        Length of individual sequences.

    Returns
    -------
    list of str
        List of strings of length `length` from `seq`.

    """
    return [seq[i: i + length] for i in range(0, len(seq), length)]


def create_dir(path):
    """Creates a new directory based on input path.

    No error if path already exists, but other error is reported.

    Parameters
    ----------
    path : str
        Path of directory to be created

    Returns
    -------
    None

    """
    try:
        os.makedirs(path)
    except OSError as exception:
        if exception.errno != errno.EEXIST:
            raise


def get_array(lang, name, index, twod=None):
    """
    Given a language and an index, returns the proper string index formatted
    into the appropriate array characters (e.g., [] or ()).

    Parameters
    ----------
    lang : str
        One of the accepted languages
    name : str
        The name of the array
    index : int
        The index to format
    twod : int, optional
        If not None and the lang is fortan or matlab this will be formatted
        as a second index in the array.
    """
    if index is None:
        #a dummy call to see if it's in shared memory
        return name

    if lang in ['fortran', 'matlab']:
        if twod is not None:
            return name +'({}, {})'.format(index + 1, twod + 1)
        return name + array_chars[lang].format(index + 1)
    return name + array_chars[lang].format(index)


def get_index(lang, index):
    """
    Given an integer index this function will return the proper string
    version of the index based on the language and other considerations

    Parameters
    ----------
    lang : str
        One of the supported languages
    index : int

    Returns
    -------
    str
        The string corresponding to the correct index to be formatted into the code
    """

    retval = None
    if lang in ['fortran', 'matlab']:
        return str(index + 1)
    if lang in ['c', 'cuda']:
        return str(index)


def reassign_species_lists(reacs, specs):
    """
    Given a list of ReacInfo, and SpecInfo's, this method will update the
    ReacInfo's reactants / products / third body list to integers
    representing the species' index in the list.
    """

    species_map = {sp.name: i for i, sp in enumerate(specs)}
    for rxn in reacs:
        rxn.reac = [species_map[sp] for sp in rxn.reac]
        rxn.prod = [species_map[sp] for sp in rxn.prod]
        rxn.thd_body_eff = [(species_map[thd[0]], thd[1]) for thd in rxn.thd_body_eff]
        if rxn.pdep_sp:
            rxn.pdep_sp = species_map[rxn.pdep_sp]


def is_integer(val):
    """Returns whether a value is an integer regardless of whether
    it is a float or an int
    """
    try:
        return val.is_integer()
    except:
        if isinstance(val, int):
            return True
        #last ditch effort
        try:
            return int(val) == float(val)
        except:
            return False


def get_parser():
    # command line arguments
    parser = ArgumentParser(description='pyJac: Generates source code '
                                        'for analytical Jacobian.'
                            )
    parser.add_argument('-l', '--lang',
                        type=str,
                        choices=langs,
                        required=True,
                        help='Programming language for output source files.'
                        )
    parser.add_argument('-i', '--input',
                        type=str,
                        required=True,
                        help='Input mechanism filename (e.g., mech.dat).'
                        )
    parser.add_argument('-t', '--thermo',
                        type=str,
                        default=None,
                        help='Thermodynamic database filename (e.g., '
                             'therm.dat), or nothing if in mechanism.'
                        )
    parser.add_argument('-ic', '--initial-conditions',
                        type=str,
                        dest='initial_conditions',
                        default='',
                        required=False,
                        help='A comma separated list of initial initial '
                             'conditions to set in the '
                             'set_same_initial_conditions method. \
                                Expected Form: T,P,Species1=...,Species2=...,...\n\
                                Temperature in K\n\
                                Pressure in Atm\n\
                                Species in moles'
                        )
    # cuda specific
    parser.add_argument('-nco', '--no-cache-optimizer',
                        dest='cache_optimizer',
                        action='store_false',
                        default=True,
                        help='Do not attempt to optimize cache store/loading '
                             'via use of a greedy selection algorithm.'
                        )
    parser.add_argument('-nosmem', '--no-shared-memory',
                        dest='no_shared',
                        action='store_true',
                        default=False,
                        help='Use this option to turn off attempted shared '
                             'memory acceleration for CUDA.'
                        )
    parser.add_argument('-pshare', '--prefer-shared',
                        dest='L1_preferred',
                        action='store_false',
                        default=True,
                        help='Use this option to allocate more space for '
                             'shared memory than the L1 cache for CUDA '
                             '(not recommended).'
                        )
    parser.add_argument('-nb', '--num-blocks',
                        type=int,
                        dest='num_blocks',
                        default=8,
                        required=False,
                        help='The target number of blocks / sm for CUDA.'
                        )
    parser.add_argument('-nt', '--num-threads',
                        type=int,
                        dest='num_threads',
                        default=64,
                        required=False,
                        help='The target number of threads / block for CUDA.'
                        )
    parser.add_argument('-mt', '--multi-threaded',
                        type=int,
                        dest='multi_thread',
                        default=None,
                        required=False,
                        help='The number of threads to use during the '
                             'optimization process.'
                        )
    parser.add_argument('-fopt', '--force-optimize',
                        dest='force_optimize',
                        action='store_true',
                        default=False,
                        help='Use this option to force a reoptimization of '
                             'the mechanism (usually only happens when '
                             'generating for a different mechanism).'
                        )
    parser.add_argument('-b', '--build_path',
                        required=False,
                        default='./out/',
                        help='The folder to generate the mechanism in.'
                        )
    parser.add_argument('-ls', '--last_species',
                        required=False,
                        type=str,
                        default=None,
                        help='The name of the species to set as the last in '
                             'the mechanism. If not specifed, defaults to '
                             'the first of N2, AR, and HE in the mechanism.'
                        )

    args = parser.parse_args()
    return args
