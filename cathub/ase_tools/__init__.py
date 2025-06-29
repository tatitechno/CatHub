import os
import sys
import collections
import math
import json
from functools import reduce
from ase import Atoms
from ase.io import read
import numpy as np
import ase
from ase.utils import formula_metal
#from ase.io.vasp import read_vasp_xml
from ase.io.vasp import __get_xml_parameter
import copy
import xml.etree.ElementTree as ET
from cathub.tools import clear_state, get_state, clear_prefactor, get_prefactor
from cathub.cathubsqlite import CathubSQLite
from collections import OrderedDict

from pathlib import Path
Path().expanduser()


accepted_formats = ['vasp-out', 'vasp-xml', 'gpaw_out', 'espresso-out', 'castep', 'crystal',
                    'ulm', 'cube', 'elk', 'gaussian', 'aims',
                    'dacapo', 'turbomole', 'db', 'json', 'traj']

PUBLICATION_TEMPLATE = collections.OrderedDict({
    'title': None,
    'authors': ['Lastname, Firstname', 'Lastname2, Firstname2'],
    'journal': 'Submitted',
    'volume': None,
    'number': None,
    'pages': None,
    'year': 2022,
    'email': 'winther@stanford.edu',
    'publisher': None,
    'doi': None,
})

REACTION_TEMPLATE = PUBLICATION_TEMPLATE.copy()
REACTION_TEMPLATE.update(collections.OrderedDict({
    'DFT_code': 'DFT CODE',
    'DFT_functionals': ['BEEF-vdW', 'HSE06'],
    'reactions': [
        collections.OrderedDict({'reactants':
                                 ['2.0H2Ogas', '-1.5H2gas', 'star'],
                                 'products': ['OOHstar@top']}),
        collections.OrderedDict({'reactants': ['CCH3star@bridge'],
                                 'products':
                                 ['Cstar@hollow', 'CH3star@ontop']}),
        collections.OrderedDict({'reactants':
                                 ['CH4gas', '-0.5H2gas', 'star'],
                                 'products': ['CH3star@ontop']})
    ],
    'bulk_compositions': ['Pt'],
    'crystal_structures': ['fcc', 'hcp'],
    'facets': ['111'],
    'energy_corrections': {},
}))


def get_chemical_formula(atoms, mode='metal'):
    """
    Compatibility function, return mode=metal, when
    available, mode=hill, when not (ASE <= 3.13)
    """
    try:
        formula = atoms.get_chemical_formula(mode=mode)
    except ValueError:
        formula = atoms.get_chemical_formula(mode='hill')
    if formula == 'HO':
        formual == 'OH'
    return formula


def get_reduced_chemical_formula(atoms):
    numbers = atoms.numbers
    reduced_numbers, den = get_reduced_numbers(numbers)
    formula = formula_metal(reduced_numbers)
    if formula == 'HO':
        formula = 'OH'
    return formula


def get_reduced_numbers(numbers):
    unique_numbers, counts = np.unique(numbers, return_counts=True)
    denominator = reduce(math.gcd, counts)
    reduced_numbers = []
    for i, atomic_number in enumerate(unique_numbers):
        reduced_count = int(counts[i] / denominator)
        reduced_numbers += [atomic_number] * reduced_count
    return reduced_numbers, denominator


def symbols(atoms):
    formula = get_chemical_formula(atoms)
    symbols = ase.symbols.string2symbols(formula)
    return ''.join(symbols)


def collect_structures(foldername,
                       verbose=False,
                       inc_pattern=[],
                       exc_pattern=[],
                       level='*'):

    if inc_pattern:
        inc_pattern = inc_pattern.split(',')
    if exc_pattern:
        exc_pattern = exc_pattern.split(',')

    if verbose:
        print(foldername)

    for i, filename in enumerate(Path(foldername).glob(level)):
        posix_filename = str(filename.as_posix())
        if verbose:
            print(i, posix_filename)
        if posix_filename.endswith('publication.txt'):
            with open(posix_filename) as infile:
                global PUBLICATION_TEMPLATE
                PUBLICATION_TEMPLATE = infile.read()
            if verbose:
                print('  -> ignore')
            continue

        elif posix_filename.endswith('traj.old'):
            if verbose:
                print('  -> ignore')
            continue
        elif Path(posix_filename).is_file():
            if inc_pattern:
                if not np.any([pat in posix_filename for pat in inc_pattern]):
                    if verbose:
                        print('  -> ignore')
                    continue
            if exc_pattern:
                if np.any([pat in posix_filename for pat in exc_pattern]):
                    if verbose:
                        print('  -> ignore')
                    continue

            try:
                filetype = ase.io.formats.filetype(posix_filename)
            except Exception as e:
                if verbose:
                    print('  -> ignore')
                continue
            if filetype in accepted_formats:
                if filetype == 'db':
                    with ase.db.connect(posix_filename) as db:
                        count = db.count()
                        print('Processing ASE db with {} structures'.format(count))
                        for row in db.select('energy'):
                            structure = row.toatoms()
                            structure.info['filename'] = row.formula + \
                                '@' + posix_filename
                            structure.info['filetype'] = filetype
                            yield structure
                else:
                    try:
                        basename = os.path.basename(
                            posix_filename).split('.')[0]
                        if basename == 'neb':  # all neb in same file
                            structures = ase.io.read(posix_filename, ':')
                            for k, s in enumerate(structures):
                                s.info['filename'] = posix_filename
                                s.info['neb_name'] = 'neb' + str(k)
                                s.info['filetype'] = filetype

                        else:
                            structures = ase.io.read(posix_filename, '-1:')
                            structures[-1].info['filename'] = posix_filename
                            structures[-1].info['filetype'] = filetype
                            if 'neb' in posix_filename:
                                structures[-1].info['neb_name'] = posix_filename.split('/')[-1].split('.')[
                                    0]

                        # to be enforced soon
                        # assert getattr(structures[-1], 'calc', None) != None, "No calculator"
                        if structures[-1].calc.parameters == {}:
                            vasprun_file = '/'.join(posix_filename.split('/')
                                                    [:-1]) + '/vasprun.xml'
                            if os.path.exists(vasprun_file):
                                parameters = read_params_xml(
                                    filename=vasprun_file)
                                structures[-1].calc.parameters = parameters
                            elif filetype == 'json':  # ASE doesn't read parameters from json :(
                                parameters = json.load(open(posix_filename, 'r'))['1']\
                                    .get('calculator_parameters', {})
                                structures[-1].calc.parameters = parameters

                                # to be enforced soon
                                #assert getattr(structures[-1].calc, 'parameters', {}) != {}, "No calculator parameters"

                        for s in structures:
                            try:
                                # ensure that the structure has an energy
                                s.get_potential_energy()
                                yield s
                            except RuntimeError:
                                if verbose:
                                    print("Did not add {posix_filename} since it has no energy"
                                          .format(
                                              posix_filename=posix_filename,))
                    except ET.ParseError:
                        print("Couldn't read XML file {posix_filename}"
                              .format(
                                  posix_filename=posix_filename,
                              ))
                    except TypeError:
                        print("Warning: Could not read {posix_filename}"
                              .format(
                                  posix_filename=posix_filename,
                              ))

                    except StopIteration:
                        print("Warning: StopIteration {posix_filename} hit."
                              .format(
                                  posix_filename=posix_filename,
                              ))
                    except IndexError:
                        print("Warning: File {posix_filename} looks incomplete"
                              .format(
                                  posix_filename=posix_filename,
                              ))
                    except OSError as e:
                        print("Error with {posix_filename}: {e}".format(
                            posix_filename=posix_filename,
                            e=e,
                        ))
                    except AssertionError as e:
                        print("Structure not accepted ({posix_filename}): {e}".format(
                            posix_filename=posix_filename,
                            e=e,
                        ))
                    except ValueError as e:
                        print("Trouble reading {posix_filename}: {e}".format(
                            posix_filename=posix_filename,
                            e=e,
                        ))
                    except DeprecationWarning as e:
                        print("Trouble reading {posix_filename}: {e}".format(
                            posix_filename=posix_filename,
                            e=e,
                        ))
                    except ImportError as e:
                        print("Trouble reading {posix_filename}: {e}".format(
                            posix_filename=posix_filename,
                            e=e,
                        ))
                    except ase.io.formats.UnknownFileTypeError as e:
                        print("Trouble reading {posix_filename}: {e}".format(
                            posix_filename=posix_filename,
                            e=e,
                        ))


def copy_atoms(atoms):
    atoms2 = atoms.copy()
    atoms2.calc = atoms.calc
    return atoms2


def get_energies(atoms_list):
    """ Potential energy for a list of atoms objects"""
    if len(atoms_list) == 1:
        return atoms_list[0].get_potential_energy()
    elif len(atoms_list) > 1:
        energies = []
        for atoms in atoms_list:
            energies.append(atoms.get_potential_energy())
        return energies


def get_atomic_numbers(atoms):
    return list(atoms.get_atomic_numbers())


def get_formula_from_numbers(numbers, mode='all'):
    formula = Atoms(numbers).get_chemical_formula(mode=mode)
    return formula


def get_numbers_from_formula(formula):
    atoms = Atoms(formula)
    return get_atomic_numbers(atoms)


def get_reaction_from_folder(folder_name):
    reaction = {}
    assert '__' in folder_name, "Please use __ as reaction arrow"

    reaction.update({'reactants': folder_name.split('__')[0].split('_'),
                     'products': folder_name.split('__')[1].split('_')})

    sites = {}
    for key, mollist in reaction.items():
        for n, mol in enumerate(mollist):
            if '@' in mol:
                mol, site = mol.split('@')
                clean_mol = clear_prefactor(mol)
                if not clean_mol in sites:
                    sites.update({clean_mol: site})
                else:
                    site0 = sites.get(clean_mol)
                    if not site0 == site:  # sites is a list
                        diff_sites = [site0, site]
                        sites.update({clean_mol: diff_sites})
                reaction[key][n] = mol
            if not np.any([state in mol for state in ['gas', 'aq', 'star']]):
                reaction[key][n] = mol + 'star'

    for key, mollist in reaction.items():
        n_star = mollist.count('star')
        if n_star > 1:
            for n in range(n_star):
                mollist.remove('star')
            mollist.append(str(n_star) + 'star')

    return reaction, sites


def get_all_atoms(molecule):
    molecule = clear_state(molecule)
    molecule, prefactor = get_prefactor(molecule)

    atoms = Atoms(molecule)

    molecule = ''.join(sorted(atoms.get_chemical_formula(mode='all')))

    return molecule, prefactor


def debug_assert(expression, message, debug=False):
    if debug:
        try:
            assert expression, message
        except AssertionError as e:
            print(e)
            return False
    else:
        assert expression, message

    return True


def compare_parameters(atoms1, atoms2):
    no_calc = False

    critical_parameters = ['encut', 'ecut', 'ediff', 'ediffg',
                           'kpts', 'gamma', 'ismear', 'sigma',
                           'ispin']
    if not atoms1.calc or not atoms2.calc:
        return 2

    if not atoms1.calc.parameters or not atoms2.calc.parameters:
        return 2

    for k in critical_parameters:
        v1 = atoms2.calc.parameters.get(k)
        v2 = atoms2.calc.parameters.get(k)

        if not np.all(v1 == v2):
            print('Parameter mismatch:', k, v, '!=', v2)
            return 0

    return 1


def read_params_xml(filename='vasprun.xml', index=-1):
    """
    Reads parameters from vasprun.xml file
    simplified version of ase.io.vasp functionality
    """
    tree = ET.iterparse(filename, events=['start', 'end'])
    atoms_init = None
    calculation = []
    ibz_kpts = None
    kpt_weights = None
    parameters = {'kpoints': {}, 'generator': {}, 'incar': {}}  # OrderedDict()
    try:
        for event, elem in tree:
            if event == 'end':
                if elem.tag == 'kpoints':
                    kpts = elem.findall("varray[@name='kpointlist']/v")
                    weights = elem.findall("varray[@name='weights']/v")
                    ibz_kpts = np.zeros((len(kpts), 3))
                    weights_array = []
                    for i, kpt in enumerate(kpts):
                        ibz_kpts[i] = [float(val) for val in kpt.text.split()]
                        weights_array += [weights[i].text.strip()]
                    parameters['kpoints']['kpointlist'] = ibz_kpts.tolist()
                    parameters['kpoints']['weights'] = weights_array
                    for subelem in elem.iter(tag='generation'):
                        kpts_params = {}
                        parameters['kpoints']['generation'] = kpts_params
                        for par in subelem.iter():
                            if par.tag in ['v', 'i']:
                                parname = par.attrib['name']
                                kpts_params[parname] = __get_xml_parameter(par)

                elif elem.tag in ['generator', 'incar']:
                    for par in elem.iter():
                        if par.tag in ['v', 'i']:
                            parname = par.attrib['name']
                            parameters[elem.tag][parname] = __get_xml_parameter(
                                par)
                elif elem.tag in ['atominfo']:
                    psp_info = []
                    for subelem in elem.iter():
                        if subelem.attrib.get('name') == 'atomtypes':
                            fieldnames = []
                            for ss in subelem.iter(tag='field'):
                                fieldnames += [ss.text]
                            for ss in subelem.iter():
                                if ss.tag == 'rc':
                                    psp_info += [{}]
                                    i = 0
                                elif ss.tag == 'c':
                                    psp_info[-1][fieldnames[i]
                                                 ] = ss.text.strip()
                                    i += 1

                    parameters['psp_info'] = psp_info
                    break
    except ET.ParseError as parse_error:

        if atoms_init is None:
            raise parse_error
        return {}
    return parameters
