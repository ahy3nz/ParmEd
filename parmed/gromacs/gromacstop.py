"""
This module contains functionality relevant to loading a GROMACS topology file
and building a Structure from it
"""
from __future__ import print_function, division, absolute_import

from parmed.constants import TINY, DEG_TO_RAD
from parmed.exceptions import GromacsError, GromacsWarning, ParameterWarning
from parmed.formats.registry import FileFormatType
from parmed.parameters import ParameterSet
from parmed.gromacs._gromacsfile import GromacsFile
from parmed.structure import Structure
from parmed.topologyobjects import (Atom, Bond, Angle, Dihedral, Improper,
            NonbondedException, ExtraPoint, BondType, Cmap, NoUreyBradley,
            AngleType, DihedralType, DihedralTypeList, ImproperType, CmapType,
            RBTorsionType, ThreeParticleExtraPointFrame, AtomType, UreyBradley,
            TwoParticleExtraPointFrame, OutOfPlaneExtraPointFrame,
            NonbondedExceptionType)
from parmed.periodic_table import element_by_mass, AtomicNum
from parmed import unit as u
from parmed.utils.io import genopen
from parmed.utils.six import add_metaclass, string_types, iteritems
from parmed.utils.six.moves import range
from collections import OrderedDict
from contextlib import closing
import copy
from datetime import datetime
import math
import os
import re
try:
    from string import letters
except ImportError:
    from string import ascii_letters as letters
import sys
import warnings

# Gromacs uses "funct" flags in its parameter files to indicate what kind of
# functional form is used for each of its different parameter types. This is
# taken from the topdirs.c source code file along with a table in the Gromacs
# user manual. The table below summarizes my findings, for reference:

# Bonds
# -----
#  1 - F_BONDS : simple harmonic potential
#  2 - F_G96BONDS : fourth-power potential
#  3 - F_MORSE : morse potential
#  4 - F_CUBICBONDS : cubic potential
#  5 - F_CONNBONDS : not even implemented in GROMACS
#  6 - F_HARMONIC : seems to be the same as (1) ??
#  7 - F_FENEBONDS : finietely-extensible-nonlinear-elastic (FENE) potential
#  8 - F_TABBONDS : bond function from tabulated function
#  9 - F_TABBONDSNC : bond function from tabulated function (no exclusions)
# 10 - F_RESTRBONDS : restraint bonds

# Angles
# ------
#  1 - F_ANGLES : simple harmonic potential
#  2 - F_G96ANGLES : cosine-based angle potential
#  3 - F_CROSS_BOND_BONDS : bond-bond cross term potential
#  4 - F_CROSS_BOND_ANGLES : bond-angle cross term potential
#  5 - F_UREY_BRADLEY : Urey-Bradley angle-bond potential
#  6 - F_QUARTIC_ANGLES : 4th-order polynomial potential
#  7 - F_TABANGLES : angle function from tabulated function
#  8 - F_LINEAR_ANGLES : angle function from tabulated function
#  9 - F_RESTRANGLES : restricted bending potential

# Dihedrals
# ---------
#  1 - F_PDIHS : periodic proper torsion potential [ k(1+cos(n*phi-phase)) ]
#  2 - F_IDIHS : harmonic improper torsion potential
#  3 - F_RBDIHS : Ryckaert-Bellemans torsion potential
#  4 - F_PIDIHS : periodic harmonic improper torsion potential (same as 1)
#  5 - F_FOURDIHS : Fourier dihedral torsion potential
#  8 - F_TABDIHS : dihedral potential from tabulated function
#  9 - F_PDIHS : Same as 1, but can be multi-term
# 10 - F_RESTRDIHS : Restricted torsion potential
# 11 - F_CBTDIHS : combined bending-torsion potential

_sectionre = re.compile(r'\[ (\w+) \]\s*$')

class _Defaults(object):
    """ Global properties of force fields as implemented in GROMACS """
    def __init__(self, nbfunc=1, comb_rule=2, gen_pairs='yes',
                 fudgeLJ=1.0, fudgeQQ=1.0):
        if int(nbfunc) not in (1, 2):
            raise ValueError('nbfunc must be 1 (L-J) or 2 (Buckingham)')
        if int(comb_rule) not in (1, 2, 3):
            raise ValueError('comb_rule must be 1, 2, or 3')
        if gen_pairs not in ('yes', 'no'):
            raise ValueError("gen_pairs must be 'yes' or 'no'")
        if float(fudgeLJ) < 0:
            raise ValueError('fudgeLJ must be non-negative')
        if float(fudgeQQ) < 0:
            raise ValueError('fudgeQQ must be non-negative')
        self.nbfunc = int(nbfunc)
        self.comb_rule = int(comb_rule)
        self.gen_pairs = gen_pairs
        self.fudgeLJ = float(fudgeLJ)
        self.fudgeQQ = float(fudgeQQ)

    def __repr__(self):
        return ('<_Defaults: nbfunc=%d, comb-rule=%d, gen-pairs="%s", '
                'fudgeLJ=%g, fudgeQQ=%g>' % (self.nbfunc, self.comb_rule,
                    self.gen_pairs, self.fudgeLJ, self.fudgeQQ))

    def __getitem__(self, idx):
        # Treat it like the array that it is in the topology file
        if idx < 0: idx += 5
        if idx == 0: return self.nbfunc
        if idx == 1: return self.comb_rule
        if idx == 2: return self.gen_pairs
        if idx == 3: return self.fudgeLJ
        if idx == 4: return self.fudgeQQ
        raise IndexError('Index %d out of range' % idx)

    def __setitem__(self, idx, value):
        if idx < 0: idx += 5
        if idx == 0:
            if int(value) not in (1, 2):
                raise ValueError('nbfunc must be 1 or 2')
            self.nbfunc = int(value)
        elif idx == 1:
            if int(value) not in (1, 2, 3):
                raise ValueError('comb_rule must be 1, 2, or 3')
            self.comb_rule = int(value)
        elif idx == 2:
            if value not in ('yes', 'no'):
                raise ValueError('gen_pairs must be "yes" or "no"')
            self.gen_pairs = value
        elif idx == 3:
            if float(value) < 0:
                raise ValueError('fudgeLJ must be non-negative')
            self.fudgeLJ = float(value)
        elif idx == 4:
            if float(value) < 0:
                raise ValueError('fudgeQQ must be non-negative')
            self.fudgeLJ = value

@add_metaclass(FileFormatType)
class GromacsTopologyFile(Structure):
    """ Class providing a parser and writer for a GROMACS topology file

    Parameters
    ----------
    fname : str
        The name of the file to read
    defines : list of str=None
        If specified, this is the set of defines to use when parsing the
        topology file
    parametrized : bool, optional
        If True, parameters are assigned after parsing is done from the
        parametertypes sections. If False, only parameter types defined in the
        parameter sections themselves are loaded (i.e., on the same line as the
        parameter was defined). Default is True
    xyz : str or array, optional
        The source of atomic coordinates. It can be a string containing the name
        of a coordinate file from which to fill the coordinates (and optionally
        the unit cell information), or it can be an array with the coordinates.
        Default is None
    box : array, optional
        If provided, the unit cell information will be set from this variable.
        If provided, it must be a collection of 6 floats representing the unit
        cell dimensions a, b, c, alpha, beta, and gamma, respectively. Default
        is None.

    Notes
    -----
    If the ``xyz`` argument is a file name that contains the unit cell
    information, this unit cell information is set. However, the ``box``
    argument takes precedence and will override values given in the coordinate
    file unless it has its default value of ``None``.
    """

    #===================================================

    @staticmethod
    def id_format(filename):
        """ Identifies the file as a GROMACS topology file

        Parameters
        ----------
        filename : str
            Name of the file to check if it is a gromacs topology file

        Returns
        -------
        is_fmt : bool
            If it is identified as a gromacs topology, return True. False
            otherwise
        """
        with closing(genopen(filename)) as f:
            for line in f:
                if line.startswith(';'):
                    line = line[:line.index(';')]
                if not line.strip(): continue
                if line.startswith('#'):
                    if line.startswith('#if'): continue
                    if line.startswith('#define'): continue
                    if line.startswith('#include'): continue
                    if line.startswith('#undef'): continue
                    if line.startswith('#endif'): continue
                    return False
                rematch = _sectionre.match(line)
                if not rematch:
                    return False
                sec, = rematch.groups()
                return sec in ('atoms', 'atomtypes', 'defaults', 'moleculetype',
                               'system', 'bondtypes', 'angletypes', 'cmaptypes',
                               'dihedraltypes', 'bonds', 'angles', 'dihedrals',
                               'cmaps', 'molecules', 'exclusions',
                               'nonbond_params', 'position_restraints')
            return False

    #===================================================

    def __init__(self, fname=None, defines=None, parametrize=True,
                 xyz=None, box=None):
        from parmed import load_file
        super(GromacsTopologyFile, self).__init__()
        self.parameterset = None
        self.defaults = _Defaults()
        if fname is not None:
            self.read(fname, defines, parametrize)
            # Fill in coordinates and unit cell information if appropriate
            if xyz is not None:
                if isinstance(xyz, string_types):
                    f = load_file(xyz)
                    if (not hasattr(f, 'coordinates') or
                            f.coordinates is None):
                        raise TypeError('File %s does not have coordinates' %
                                        xyz)
                    self.coordinates = f.coordinates
                    if box is None and hasattr(f, 'box'):
                        self.box = f.box
                else:
                    self.coordinates = xyz
            if box is not None:
                self.box = box
            self.unchange()
        elif xyz is not None or box is not None:
            raise ValueError('Cannot provide coordinates/box and NOT a top')

    #===================================================

    def read(self, fname, defines=None, parametrize=True):
        """ Reads the topology file into the current instance """
        from parmed import gromacs as gmx
        params = self.parameterset = ParameterSet()
        molecules = dict()
        bond_types = dict()
        angle_types = dict()
        ub_types = dict()
        dihedral_types = dict()
        exc_types = dict()
        structure_contents = []
        if defines is None:
            defines = OrderedDict(FLEXIBLE=1)
        proper_multiterm_dihedrals = dict()
        with closing(GromacsFile(fname, includes=[gmx.GROMACS_TOPDIR],
                                 defines=defines)) as f:
            current_section = None
            for line in f:
                line = line.strip()
                if not line: continue

                if line[0] == '[':
                    current_section = line[1:-1].strip()
                elif current_section == 'moleculetype':
                    molname, nrexcl = line.split()
                    nrexcl = int(nrexcl)
                    if molname in molecules:
                        raise GromacsError('Duplicate definition of molecule %s'
                                           % molname)
                    molecule = Structure()
                    molecules[molname] = (molecule, nrexcl)
                    molecule.nrexcl = nrexcl
                    bond_types = dict()
                    angle_types = dict()
                    ub_types = dict()
                    dihedral_types = dict()
                    exc_types = dict()
                elif current_section == 'atoms':
                    words = line.split()
                    try:
                        attype = params.atom_types[words[1]]
                    except KeyError:
                        attype = None
                    if len(words) < 8:
                        if attype is not None:
                            mass = attype.mass
                            atomic_number = attype.atomic_number
                        else:
                            mass = -1
                            atomic_number = -1
                    else:
                        mass = float(words[7])
                        if attype is not None and attype.atomic_number >= 0:
                            atomic_number = attype.atomic_number
                        else:
                            atomic_number = AtomicNum[element_by_mass(mass)]
                    if len(words) < 7:
                        charge = None
                    else:
                        charge = float(words[6])
                    if atomic_number == 0:
                        atom = ExtraPoint(name=words[4], type=words[1],
                                          charge=charge)
                    else:
                        atom = Atom(atomic_number=atomic_number, name=words[4],
                                    type=words[1], charge=charge, mass=mass)
                    molecule.add_atom(atom, words[3], int(words[2]))
                elif current_section == 'bonds':
                    words = line.split()
                    i, j = int(words[0])-1, int(words[1])-1
                    funct = int(words[2])
                    if funct != 1:
                        warnings.warn('bond funct != 1; unknown functional',
                                      GromacsWarning)
                        self.unknown_functional = True
                    molecule.bonds.append(Bond(molecule.atoms[i],
                                               molecule.atoms[j]))
                    molecule.bonds[-1].funct = funct
                    if len(words) >= 5 and funct == 1:
                        req, k = (float(x) for x in words[3:5])
                        if (req, k) in bond_types:
                            bt = bond_types[(req, k)]
                        else:
                            bt = BondType(
                                    k*u.kilojoule_per_mole/u.nanometer**2/2,
                                    req*u.nanometer, list=molecule.bond_types
                            )
                            molecule.bond_types.append(bt)
                            bond_types[(req, k)] = bt
                        molecule.bonds[-1].type= bt
                elif current_section == 'pairs':
                    words = line.split()
                    i, j = int(words[0])-1, int(words[1])-1
                    funct = int(words[2])
                    if funct != 1:
                        # This is not even supported in Gromacs
                        warnings.warn('pairs funct != 1; unknown functional',
                                      GromacsWarning)
                        self.unknown_functional = True
                    molecule.adjusts.append(
                            NonbondedException(molecule.atoms[i],
                                               molecule.atoms[j])
                    )
                    molecule.adjusts[-1].funct = funct
                    if funct == 1 and len(words) >= 5:
                        sig = float(words[3]) * 2**(1/6)
                        eps = float(words[4])
                        if (sig, eps) in exc_types:
                            nt = exc_types[(sig, eps)]
                        else:
                            nt = NonbondedExceptionType(
                                    sig*u.nanometers,
                                    eps*u.kilojoules_per_mole,
                                    self.defaults.fudgeQQ,
                                    list=molecule.adjust_types
                            )
                            molecule.adjust_types.append(nt)
                            exc_types[(sig, eps)] = nt
                        molecule.adjusts[-1].type = nt
                elif current_section == 'angles':
                    words = line.split()
                    i, j, k = [int(w)-1 for w in words[:3]]
                    funct = int(words[3])
                    if funct not in (1, 5):
                        warnings.warn('angles funct != 1 or 5; unknown '
                                      'functional', GromacsWarning)
                        self.unknown_functional = True
                    molecule.angles.append(
                            Angle(molecule.atoms[i], molecule.atoms[j],
                                  molecule.atoms[k])
                    )
                    if funct == 5:
                        molecule.urey_bradleys.append(
                                UreyBradley(molecule.atoms[i],molecule.atoms[k])
                        )
                    molecule.angles[-1].funct = funct
                    if funct == 1 and len(words) >= 6:
                        theteq, k = (float(x) for x in words[4:6])
                        if (theteq, k) in angle_types:
                            at = angle_types[(theteq, k)]
                        else:
                            at = AngleType(k*u.kilojoule_per_mole/u.radian**2/2,
                                    theteq*u.degree, list=molecule.angle_types)
                            molecule.angle_types.append(at)
                            angle_types[(theteq, k)] = at
                        molecule.angles[-1].type = at
                    elif funct == 5 and len(words) >= 8:
                        theteq, k, ubreq, ubk = (float(x) for x in words[4:8])
                        at = AngleType(k*u.kilojoule_per_mole/u.radian**2/2,
                                theteq*u.degree, list=molecule.angle_types)
                        molecule.angle_types.append(at)
                        molecule.angles[-1].type = at
                        if ubreq > 0 and ubk > 0:
                            if (ubreq, ubk) in ub_types:
                                ubt = ub_types[(ubreq, ubk)]
                            else:
                                ubt = BondType(
                                    ubk*u.kilojoule_per_mole/u.nanometer**2/2,
                                    ubreq*u.nanometer,
                                    list=molecule.urey_bradley_types
                                )
                                molecule.urey_bradley_types.append(ubt)
                                ub_types[(ubreq, ubk)] = ubt
                        else:
                            ubt = NoUreyBradley
                        molecule.urey_bradleys[-1].type = ubt
                elif current_section == 'dihedrals':
                    words = line.split()
                    i, j, k, l = [int(x)-1 for x in words[:4]]
                    funct = int(words[4])
                    if funct in (1, 4) or (funct == 9 and len(words) < 8):
                        # Normal dihedral
                        improper = funct == 4
                        dih = Dihedral(molecule.atoms[i], molecule.atoms[j],
                                       molecule.atoms[k], molecule.atoms[l],
                                       improper=improper)
                        molecule.dihedrals.append(dih)
                        molecule.dihedrals[-1].funct = funct
                    elif funct == 9:
                        atoms = tuple(molecule.atoms[int(x)-1]
                                      for x in words[:4])
                        phase, phi_k, per = (float(x) for x in words[5:8])
                        dt = DihedralType(phi_k*u.kilojoule_per_mole,
                                          per, phase*u.degrees,
                                          scee=1/self.defaults.fudgeQQ,
                                          scnb=1/self.defaults.fudgeLJ)
                        if atoms in proper_multiterm_dihedrals:
                            for edt in proper_multiterm_dihedrals[atoms]:
                                if edt.per == dt.per:
                                    raise GromacsError(
                                        'duplicate periodicity term found '
                                        'in inline dihedral parameter for '
                                        'atoms [%s]' % ', '.join(words[:4])
                                    )
                            proper_multiterm_dihedrals[atoms].append(dt)
                        else:
                            dt = DihedralType(phi_k*u.kilojoule_per_mole,
                                              per, phase*u.degrees,
                                              scee=1/self.defaults.fudgeQQ,
                                              scnb=1/self.defaults.fudgeLJ)
                            dtl = DihedralTypeList()
                            dtl.append(dt)
                            dtl.list = molecule.dihedral_types
                            molecule.dihedral_types.append(dtl)
                            dih = Dihedral(*atoms, improper=False, type=dtl)
                            molecule.dihedrals.append(dih)
                            proper_multiterm_dihedrals[atoms] = dtl
                            proper_multiterm_dihedrals[tuple(reversed(atoms))] = dtl
                    elif funct == 2:
                        # Improper
                        imp = Improper(molecule.atoms[i], molecule.atoms[j],
                                       molecule.atoms[k], molecule.atoms[l])
                        molecule.impropers.append(imp)
                        molecule.impropers[-1].funct = funct
                    elif funct == 3:
                        rb = Dihedral(molecule.atoms[i], molecule.atoms[j],
                                      molecule.atoms[k], molecule.atoms[l])
                        molecule.rb_torsions.append(rb)
                        molecule.rb_torsions[-1].funct = funct
                    else:
                        # ??? unknown
                        warnings.warn('torsions funct != 1, 2, 3, 4, 9; unknown'
                                      ' functional', GromacsWarning)
                        dih = Dihedral(molecule.atoms[i], molecule.atoms[j],
                                       molecule.atoms[k], molecule.atoms[l])
                        molecule.dihedrals.append(dih)
                        molecule.dihedrals[-1].funct == funct
                        self.unknown_functional = True
                    if funct in (1, 4) and len(words) >= 8:
                        phase, phi_k, per = (float(x) for x in words[5:8])
                        if (phase, phi_k, per) in dihedral_types:
                            dt = dihedral_types[(phase, phi_k, per)]
                        else:
                            dt = DihedralType(phi_k*u.kilojoule_per_mole,
                                              per, phase*u.degrees,
                                              scee=1/self.defaults.fudgeQQ,
                                              scnb=1/self.defaults.fudgeLJ,
                                              list=molecule.dihedral_types)
                            molecule.dihedral_types.append(dt)
                            dihedral_types[(phase, phi_k, per)] = dt
                        molecule.dihedrals[-1].type = dt
                    elif funct == 2 and len(words) >= 7:
                        psieq, k = (float(x) for x in words[5:7])
                        if (psieq, k) in dihedral_types:
                            dt = dihedral_types[(psieq, k)]
                        else:
                            dt = ImproperType(k*u.kilojoule_per_mole/u.radian**2/2,
                                    psieq*u.degree, list=molecule.improper_types)
                            molecule.improper_types.append(dt)
                            dihedral_types[(psieq, k)] = dt
                        molecule.impropers[-1].type = dt
                    elif funct == 3 and len(words) >= 11:
                        c0, c1, c2, c3, c4, c5 = (float(x) for x in words[5:11])
                        if (c0, c1, c2, c3, c4, c5) in dihedral_types:
                            dt = dihedral_types[(c0, c1, c2, c3, c4, c5)]
                        else:
                            kjpm = u.kilojoules_per_mole
                            dt = RBTorsionType(c0*kjpm, c1*kjpm, c2*kjpm,
                                               c3*kjpm, c4*kjpm, c5*kjpm,
                                               scee=1/self.defaults.fudgeQQ,
                                               scnb=1/self.defaults.fudgeLJ,
                                               list=molecule.rb_torsion_types)
                            molecule.rb_torsion_types.append(dt)
                            dihedral_types[(c0, c1, c2, c3, c4, c5)] = dt
                        molecule.rb_torsions[-1].type = dt
                elif current_section == 'cmap':
                    words = line.split()
                    i, j, k, l, m = (int(w)-1 for w in words[:5])
                    funct = int(words[5])
                    if funct != 1:
                        warnings.warn('cmap funct != 1; unknown functional',
                                      GromacsWarning)
                        self.unknown_functional = True
                    cmap = Cmap(molecule.atoms[i], molecule.atoms[j],
                                molecule.atoms[k], molecule.atoms[l],
                                molecule.atoms[m])
                    molecule.cmaps.append(cmap)
                    molecule.cmaps[-1].funct = funct
                elif current_section == 'system':
                    self.title = line
                elif current_section == 'defaults':
                    words = line.split()
                    if len(words) < 4:
                        raise GromacsError('Too few fields in [ defaults ]')
                    if words[0] != '1':
                        warnings.warn('Unsupported nonbonded type; unknown '
                                      'functional', GromacsWarning)
                        self.unknown_functional = True
                    if words[1] in ('1', '3'):
                        self.combining_rule = 'geometric'
                    self.defaults = _Defaults(*words)
                elif current_section == 'molecules':
                    name, num = line.split()
                    num = int(num)
                    structure_contents.append((name, num))
                elif current_section == 'settles':
                    # Instead of adding bonds that get constrained for waters
                    # (or other 3-atom molecules), GROMACS uses a "settles"
                    # section to specify the constraint geometry. We have to
                    # translate that into bonds.
                    natoms = len([a for a in molecule.atoms
                                    if not isinstance(a, ExtraPoint)])
                    if natoms != 3:
                        raise GromacsError("Cannot SETTLE a %d-atom molecule" %
                                           natoms)
                    try:
                        oxy, = [atom for atom in molecule.atoms
                                    if atom.atomic_number == 8]
                        hyd1, hyd2 = [atom for atom in molecule.atoms
                                        if atom.atomic_number == 1]
                    except ValueError:
                        raise GromacsError('Can only SETTLE water; Could not '
                                           'detect 2 hydrogens and 1 oxygen')
                    #TODO see if there's a bond_type entry in the parameter set
                    #     that we can fill in
                    try:
                        i, funct, doh, dhh = line.split()
                        doh, dhh = float(doh), float(dhh)
                    except ValueError:
                        raise GromacsError('Bad [ settles ] line')
                    bt_oh = BondType(5e5*u.kilojoules_per_mole/u.nanometers**2,
                                     doh*u.nanometers, list=molecule.bond_types)
                    bt_hh = BondType(5e5*u.kilojoules_per_mole/u.nanometers**2,
                                     dhh*u.nanometers, list=molecule.bond_types)
                    molecule.bond_types.extend([bt_oh, bt_hh])
                    molecule.bonds.append(Bond(oxy, hyd1, bt_oh))
                    molecule.bonds.append(Bond(oxy, hyd2, bt_oh))
                    molecule.bonds.append(Bond(hyd1, hyd2, bt_hh))
                elif current_section in ('virtual_sites3', 'dummies3'):
                    words = line.split()
                    vsite = molecule.atoms[int(words[0])-1]
                    atoms = [molecule.atoms[int(i)-1] for i in words[1:4]]
                    funct = int(words[4])
                    if funct == 1:
                        a, b = float(words[5]), float(words[6])
                        if abs(a - b) > TINY:
                            raise GromacsError('Cannot handle virtual site '
                                               'frames with different weights')
                    else:
                        raise GromacsError('Only 3-point virtual site type "1" '
                                           'is supported')
                    # We need to know the geometry of the frame in order to
                    # determine the bond length between the virtual site and its
                    # parent atom
                    parent = atoms[0]
                    foundt = False
                    kws = dict()
                    for bond in parent.bonds:
                        if atoms[1] in bond:
                            if bond.type is None:
                                key = (parent.type, atoms[1].type)
                                if key not in params.bond_types:
                                    raise GromacsError(
                                            'Cannot determine geometry of '
                                            'virtual site without bond types'
                                    )
                                kws['dp1'] = params[key].req
                        if atoms[2] in bond:
                            if bond.type is None:
                                key = (_gettype(bond.atom1),
                                       _gettype(bond.atom2))
                                if key not in params.bond_types:
                                    raise GromacsError(
                                            'Cannot determine geometry of '
                                            'virtual site without bond types'
                                    )
                                kws['dp2'] = params.bond_types[key].req
                    for angle in parent.angles:
                        if parent is not angle.atom2: continue
                        if atoms[0] not in angle or atoms[1] not in angle:
                            continue
                        foundt = True
                        if angle.type is None:
                            key = (_gettype(angle.atom1), _gettype(angle.atom2),
                                   _gettype(angle.atom3))
                            if key not in params.angle_types:
                                raise GromacsError(
                                        'Cannot determine geometry of '
                                        'virtual site without bond types'
                                )
                            kws['theteq'] = params.angle_types[key].theteq
                    if not foundt:
                        for bond in atoms[1].bonds:
                            if atoms[2] in bond:
                                if bond.type is None:
                                    key = (_gettype(bond.atom1),
                                           _gettype(bond.atom2))
                                    if key not in params.bond_types:
                                        raise GromacsError(
                                            'Cannot determine geometry of '
                                            'virtual site without bond types'
                                        )
                                    kws['d12'] = params.bond_types[key].req
                    bondlen = ThreeParticleExtraPointFrame.from_weights(parent,
                            atoms[1], atoms[2], a, b, **kws)
                    bt_vs = BondType(0, bondlen*u.angstroms,
                                     list=molecule.bond_types)
                    if vsite in parent.bond_partners:
                        raise GromacsError('Unexpected bond b/w virtual site '
                                           'and its parent')
                    molecule.bonds.append(Bond(vsite, parent, bt_vs))
                    molecule.bond_types.append(bt_vs)
                elif current_section == 'exclusions':
                    atoms = [molecule.atoms[int(w)-1] for w in line.split()]
                    for a in atoms[1:]:
                        atoms[0].exclude(a)
                elif current_section == 'atomtypes':
                    words = line.split()
                    # Support the following spec, found in the Gromacs source
                    # code:
                    # Field 0 (mandatory) : nonbonded type name (string)
                    # Field 1 (optional)  : bonded type (string)
                    # Field 2 (optional)  : atomic number (int)
                    # Field 3 (mandatory) : mass (float)
                    # Field 4 (mandatory) : charge (float)
                    # Field 5 (mandatory) : particle type (single character)
                    attype = words[0]
                    if len(words[3]) == 1 and words[3] in letters:
                        atnum = -1
                        sigidx = 4
                        ptypeidx = 3
                        massidx = 1
                        bond_type = None
                    elif len(words[5]) == 1 and words[5] in letters:
                        sigidx = 6
                        ptypeidx = 5
                        massidx = 3
                        atnum = int(words[2])
                        bond_type = words[1]
                    else:
                        ptypeidx = 4
                        massidx = 2
                        sigidx = 5
                        try:
                            atnum = int(words[1])
                            bond_type = None
                        except ValueError:
                            # This must be a bonded type string
                            bond_type = words[1]
                            atnum = -1
                    mass = float(words[massidx])
                    if mass > 0 and atnum == -1:
                        atnum = AtomicNum[element_by_mass(mass)]
#                   chg = float(words[3])
                    ptype = words[ptypeidx]
                    sig = float(words[sigidx]) * u.nanometers
                    eps = float(words[sigidx+1]) * u.kilojoules_per_mole
                    typ = AtomType(attype, None, mass, atnum,
                                   bond_type=bond_type)
                    typ.set_lj_params(eps, sig*2**(1/6)/2)
                    params.atom_types[attype] = typ
                elif current_section == 'nonbond_params':
                    words = line.split()
                    a1, a2 = words[:2]
#                   func = int(words[2])
                    sig, eps = (float(x) for x in words[3:5])
                    sig *= 10 # Convert to Angstroms
                    eps *= u.kilojoule.conversion_factor_to(u.kilocalorie)
                    params.nbfix_types[(a1, a2)] = (eps, sig*2**(1/6))
                    params.nbfix_types[(a2, a1)] = (eps, sig*2**(1/6))
                    params.atom_types[a1].add_nbfix(a2, sig*2**(1/6), eps)
                    params.atom_types[a2].add_nbfix(a1, sig*2**(1/6), eps)
                elif current_section == 'bondtypes':
                    words = line.split()
                    r = float(words[3]) * u.nanometers
                    k = (float(words[4]) / 2) * (
                            u.kilojoules_per_mole / u.nanometers**2)
                    if words[2] != '1':
                        warnings.warn('bondtypes funct != 1; unknown '
                                      'functional', GromacsWarning)
                        self.unknown_functional = True
                    ptype = BondType(k, r)
                    params.bond_types[(words[0], words[1])] = ptype
                    params.bond_types[(words[1], words[0])] = ptype
                elif current_section == 'angletypes':
                    words = line.split()
                    theta = float(words[4]) * u.degrees
                    k = (float(words[5]) / 2) * (
                            u.kilojoules_per_mole / u.radians**2)
                    if words[3] != '1' and words[3] != '5':
                        warnings.warn('angletypes funct != 1 or 5; unknown '
                                      'functional', GromacsWarning)
                        self.unknown_functional = True
                    if words[3] == '5':
                        # Contains the angle with urey-bradley
                        ub0 = float(words[6])
                        cub = float(words[7]) / 2
                        if cub == 0:
                            ub = NoUreyBradley
                        else:
                            ub0 *= u.nanometers
                            cub *= u.kilojoules_per_mole / u.nanometers**2
                            ub = BondType(cub, ub0)
                        params.urey_bradley_types[(words[0], words[2])] = ub
                        params.urey_bradley_types[(words[2], words[0])] = ub
                    ptype = AngleType(k, theta)
                    params.angle_types[(words[0], words[1], words[2])] = ptype
                    params.angle_types[(words[2], words[1], words[0])] = ptype
                elif current_section == 'dihedraltypes':
                    words = line.split()
                    replace = False
                    dtype = 'normal'
                    # Ugh. Gromacs allows only two atom types (the middle atom
                    # types) to be specified. We have to figure out if that's
                    # the case here.
                    middle_only = words[2] in ('1', '2', '3', '4', '5', '8',
                                               '9', '10', '11')
                    if middle_only:
                        a1 = a4 = 'X'
                        a2, a3 = words[:2]
                        si = 2
                    else:
                        a1, a2, a3, a4 = words[:4]
                        si = 4
                    improper_periodic = False
                    if words[si] == '1':
                        pass
                    elif words[si] == '4':
                        replace = True
                        improper_periodic = True
                    elif words[si] == '9':
                        pass
                    elif words[si] == '2':
                        replace = True
                        dtype = 'improper'
                    elif words[si] == '3':
                        dtype = 'rbtorsion'
                    else:
                        warnings.warn('dihedraltypes funct not supported',
                                      GromacsWarning)
                        self.unknown_functional = True
                    # Do the proper types
                    if dtype == 'normal':
                        phase = float(words[si+1]) * u.degrees
                        phi_k = float(words[si+2]) * u.kilojoules_per_mole
                        per = int(words[si+3])
                        dt = DihedralType(phi_k, per, phase,
                                          scee=1/self.defaults.fudgeQQ,
                                          scnb=1/self.defaults.fudgeLJ)
                        key = (a1, a2, a3, a4)
                        rkey = (a4, a3, a2, a1)
                        if improper_periodic:
                            # Impropers only ever have 1 term, and therefore
                            # always replace.
                            params.improper_periodic_types[key] = dt
                            params.improper_periodic_types[rkey] = dt
                        else:
                            if replace or not key in params.dihedral_types:
                                dtl = DihedralTypeList()
                                dtl.append(dt)
                                params.dihedral_types[key] = dtl
                                params.dihedral_types[rkey] = dtl
                            else:
                                params.dihedral_types[key].append(dt)
                    elif dtype == 'improper':
                        theta = float(words[si+1])*u.degrees
                        k = float(words[si+2])*u.kilojoules_per_mole/u.radians**2/2
                        a1, a2, a3, a4 = sorted([a1, a2, a3, a4])
                        ptype = ImproperType(k, theta)
                        params.improper_types[(a1, a2, a3, a4)] = ptype
                    elif dtype == 'rbtorsion':
                        a1, a2, a3, a4 = words[:4]
                        c0, c1, c2, c3, c4, c5 = [float(x)*u.kilojoules_per_mole
                                                    for x in words[si+1:si+7]]
                        ptype = RBTorsionType(c0, c1, c2, c3, c4, c5,
                                              scee=1/self.defaults.fudgeQQ,
                                              scnb=1/self.defaults.fudgeLJ)
                        params.rb_torsion_types[(a1, a2, a3, a4)] = ptype
                        params.rb_torsion_types[(a4, a3, a2, a1)] = ptype
                elif current_section == 'cmaptypes':
                    words = line.split()
                    a1, a2, a3, a4, a5 = words[:5]
                    funct = int(words[5])
                    res1, res2 = int(words[6]), int(words[7])
                    grid = [float(w) for w in words[8:]] * u.kilojoules_per_mole
                    if len(grid) != res1 * res2:
                        raise GromacsError('CMAP grid dimensions do not match '
                                           'resolution')
                    if res1 != res2:
                        raise GromacsError('Only square CMAPs are supported')
                    cmaptype = CmapType(res1, grid)
                    params.cmap_types[(a1, a2, a3, a4, a5)] = cmaptype
                    params.cmap_types[(a5, a4, a3, a2, a1)] = cmaptype
                elif current_section == 'pairtypes':
                    words = line.split()
                    a1, a2 = words[:2]
                    funct = int(words[2])
                    cs6, cs12 = (float(x) for x in words[3:5])
                    cs6 *= u.nanometers * 2**(1/6)
                    cs12 *= u.kilojoules_per_mole
                    pairtype = NonbondedExceptionType(cs6, cs12,
                                self.defaults.fudgeQQ, list=self.adjust_types)
                    self.adjust_types.append(pairtype)
                    params.pair_types[(a1, a2)] = pairtype
                    params.pair_types[(a2, a1)] = pairtype
            itplist = f.included_files

        # Combine first, then parametrize. That way, we don't have to create
        # copies of the ParameterType instances in self.parameterset
        for molname, num in structure_contents:
            if molname not in molecules:
                raise GromacsError('Structure contains %s molecules, but no '
                                   'template defined' % molname)
            molecule, nrexcl = molecules[molname]
            if nrexcl < 3 and _any_atoms_farther_than(molecule, nrexcl):
                warnings.warn('nrexcl %d not currently supported' % nrexcl,
                              GromacsWarning)
            elif nrexcl > 3 and _any_atoms_farther_than(molecule, 3):
                warnings.warn('nrexcl %d not currently supported' % nrexcl,
                              GromacsWarning)
            if num == 0:
                warnings.warn('Detected addition of 0 %s molecules in topology '
                              'file' % molname, GromacsWarning)
            if num == 1:
                self += molecules[molname][0]
            elif num > 1:
                self += molecules[molname][0] * num
            else:
                raise GromacsError("Can't add %d %s molecules" % (num, molname))
        self.itps = itplist
        self.parametrize()

    #===================================================

    def parametrize(self):
        """
        Assign parameters to the current structure. This should be called
        *after* `read`
        """
        if self.parameterset is None:
            raise RuntimeError('parametrize called before read')
        params = copy.copy(self.parameterset)
        def update_typelist_from(ptypes, types):
            added_types = set(id(typ) for typ in types)
            for k, typ in iteritems(ptypes):
                if not typ.used: continue
                if id(typ) in added_types: continue
                added_types.add(id(typ))
                types.append(typ)
            types.claim()
        # Assign all of the parameters. If they've already been assigned (i.e.,
        # on the parameter line itself) keep the existing parameters
        for atom in self.atoms:
            atom.atom_type = params.atom_types[atom.type]
        # The list of ordered 2-tuples of atoms explicitly specified in [ pairs ].
        # Under most circumstances, this is the list of 1-4 pairs.
        gmx_pair = set()
        for pair in self.adjusts:
            if pair.atom1 > pair.atom2:
                gmx_pair.add((pair.atom2, pair.atom1))
            else:
                gmx_pair.add((pair.atom1, pair.atom2))
            if pair.type is not None: continue
            key = (_gettype(pair.atom1), _gettype(pair.atom2))
            if key in params.pair_types:
                pair.type = params.pair_types[key]
                pair.type.used = True
            elif self.defaults.gen_pairs:
                if self.combining_rule == 'geometric':
                    eps = math.sqrt(pair.atom1.epsilon * pair.atom2.epsilon)
                    sig = math.sqrt(pair.atom1.sigma * pair.atom2.sigma)
                elif self.combining_rule == 'lorentz':
                    eps = math.sqrt(pair.atom1.epsilon * pair.atom2.epsilon)
                    sig = 0.5 * (pair.atom1.sigma + pair.atom2.sigma)
                else:
                    assert False, 'Unrecognized combining rule'
                eps *= self.defaults.fudgeLJ
                pairtype = NonbondedExceptionType(sig*2**(1/6), eps,
                            self.defaults.fudgeQQ, list=self.adjust_types)
                self.adjust_types.append(pairtype)
                pair.type = pairtype
                pair.type.used = True
            else:
                warnings.warn('Not all pair parameters can be found',
                              ParameterWarning)
        update_typelist_from(params.pair_types, self.adjust_types)
        # This is the list of 1-4 pairs determined from the bond graph.
        # If this is different from what's in [ pairs ], we print a warning
        # and make some adjustments (specifically, other programs assume
        # the 1-4 list is complete, so we zero out the parameters for
        # 1-4 pairs that aren't in [ pairs ].
        true_14 = set()
        for bond in self.bonds:
            for bpi in bond.atom1.bond_partners:
                for bpj in bond.atom2.bond_partners:
                    if len(set([bpi, bond.atom1, bond.atom2, bpj])) < 4:
                        continue
                    if bpi in bpj.bond_partners or bpi in bpj.angle_partners:
                        continue
                    if bpi > bpj:
                        true_14.add((bpj, bpi))
                    else:
                        true_14.add((bpi, bpj))
            if bond.type is not None: continue
            key = (_gettype(bond.atom1), _gettype(bond.atom2))
            if key in params.bond_types:
                bond.type = params.bond_types[key]
                bond.type.used = True
            else:
                warnings.warn('Not all bond parameters found',
                              ParameterWarning)
        if len(true_14 - gmx_pair) > 0:
            zero_pairtype = NonbondedExceptionType(0.0, 0.0, 0.0,
                                                   list=self.adjust_types)
            self.adjust_types.append(zero_pairtype)
            num_zero_14 = 0
            for a1, a2 in (true_14 - gmx_pair):
                self.adjusts.append(NonbondedException(a1, a2, zero_pairtype))
                num_zero_14 += 1
            warnings.warn('%i 1-4 pairs were missing from the [ pairs ] '
                          'section and were set to zero; make sure you '
                          'know what you\'re doing!' % num_zero_14,
                          GromacsWarning)
        if len(gmx_pair - true_14) > 0:
            warnings.warn('The [ pairs ] section contains %i exceptions that '
                          'aren\'t 1-4 pairs; make sure you know what '
                          'you\'re doing!' % (len(gmx_pair - true_14)),
                          GromacsWarning)
        update_typelist_from(params.bond_types, self.bond_types)
        for angle in self.angles:
            if angle.type is not None: continue
            key = (_gettype(angle.atom1), _gettype(angle.atom2),
                   _gettype(angle.atom3))
            if key in params.angle_types:
                angle.type = params.angle_types[key]
                angle.type.used = True
            else:
                warnings.warn('Not all angle parameters found',
                              ParameterWarning)
        update_typelist_from(params.angle_types, self.angle_types)
        for ub in self.urey_bradleys:
            if ub.type is not None: continue
            key = (_gettype(ub.atom1), _gettype(ub.atom2))
            if key in params.urey_bradley_types:
                ub.type = params.urey_bradley_types[key]
                if ub.type is not NoUreyBradley:
                    ub.type.used = True
            else:
                warnings.warn('Not all urey-bradley parameters found',
                              ParameterWarning)
        # Now strip out all of the Urey-Bradley terms whose parameters are 0
        for i in reversed(range(len(self.urey_bradleys))):
            if self.urey_bradleys[i].type is NoUreyBradley:
                del self.urey_bradleys[i]
        update_typelist_from(params.urey_bradley_types, self.urey_bradley_types)
        for t in self.dihedrals:
            if t.type is not None: continue
            key = (_gettype(t.atom1), _gettype(t.atom2), _gettype(t.atom3),
                   _gettype(t.atom4))
            if not t.improper:
                wckey = ('X', _gettype(t.atom2), _gettype(t.atom3), 'X')
                wckey1 = (_gettype(t.atom1), _gettype(t.atom2),
                          _gettype(t.atom3), 'X')
                wckey2 = ('X', _gettype(t.atom2), _gettype(t.atom3),
                          _gettype(t.atom4))
                if key in params.dihedral_types:
                    t.type = params.dihedral_types[key]
                    t.type.used = True
                elif wckey1 in params.dihedral_types:
                    t.type = params.dihedral_types[wckey1]
                    t.type.used = True
                elif wckey2 in params.dihedral_types:
                    t.type = params.dihedral_types[wckey2]
                    t.type.used = True
                elif wckey in params.dihedral_types:
                    t.type = params.dihedral_types[wckey]
                    t.type.used = True
                else:
                    warnings.warn('Not all torsion parameters found',
                                  ParameterWarning)
            else:
                if key in params.improper_periodic_types:
                    t.type = params.improper_periodic_types[key]
                    t.type.used = True
                else:
                    for wckey in [(key[0],key[1],key[2],'X'),
                                  ('X',key[1],key[2],key[3]),
                                  (key[0],key[1],'X','X'),
                                  ('X','X',key[2],key[3])]:
                        if wckey in params.improper_periodic_types:
                            t.type = params.improper_periodic_types[wckey]
                            t.type.used = True
                            break
                    else:
                        warnings.warn('Not all improper torsion parameters '
                                      'found', ParameterWarning)
        update_typelist_from(params.dihedral_types, self.dihedral_types)
        update_typelist_from(params.improper_periodic_types, self.dihedral_types)
        for t in self.rb_torsions:
            if t.type is not None: continue
            key = (_gettype(t.atom1), _gettype(t.atom2), _gettype(t.atom3),
                   _gettype(t.atom4))
            wckey = ('X', _gettype(t.atom2), _gettype(t.atom3), 'X')
            wckey1 = (_gettype(t.atom1), _gettype(t.atom2),
                      _gettype(t.atom3), 'X')
            wckey2 = ('X', _gettype(t.atom2), _gettype(t.atom3),
                      _gettype(t.atom4))
            if key in params.rb_torsion_types:
                t.type = params.rb_torsion_types[key]
                t.type.used = True
            elif wckey1 in params.rb_torsion_types:
                t.type = params.rb_torsion_types[wckey1]
                t.type.used = True
            elif wckey2 in params.rb_torsion_types:
                t.type = params.rb_torsion_types[wckey2]
                t.type.used = True
            elif wckey in params.rb_torsion_types:
                t.type = params.rb_torsion_types[wckey]
                t.type.used = True
            else:
                warnings.warn('Not all R-B torsion parameters found',
                              ParameterWarning)
        update_typelist_from(params.rb_torsion_types, self.rb_torsion_types)
        self.update_dihedral_exclusions()
        for t in self.impropers:
            if t.type is not None: continue
            key = tuple(sorted([_gettype(t.atom1), _gettype(t.atom2),
                                _gettype(t.atom3), _gettype(t.atom4)]))
            if key in params.improper_types:
                t.type = params.improper_types[key]
                t.type.used = True
                continue
            # Now we will try to find a compatible wild-card... the first atom
            # is the central atom. So take each of the other three and plug that
            # one in
            for anchor in (_gettype(t.atom2), _gettype(t.atom3),
                           _gettype(t.atom4)):
                wckey = tuple(sorted([_gettype(t.atom1), anchor, 'X', 'X']))
                if wckey not in params.improper_types: continue
                t.type = params.improper_types[wckey]
                t.type.used = True
                break
            else:
                warnings.warn('Not all quadratic improper parameters found',
                              ParameterWarning)
        update_typelist_from(params.improper_types, self.improper_types)
        for c in self.cmaps:
            if c.type is not None: continue
            key = (_gettype(c.atom1), _gettype(c.atom2), _gettype(c.atom3),
                    _gettype(c.atom4), _gettype(c.atom5))
            if key in params.cmap_types:
                c.type = params.cmap_types[key]
                c.type.used = True
            else:
                warnings.warn('Not all cmap parameters found', ParameterWarning)
        update_typelist_from(params.cmap_types, self.cmap_types)

    #===================================================

    @classmethod
    def from_structure(cls, struct, copy=False):
        """ Instantiates a GromacsTopologyFile instance from a Structure

        Parameters
        ----------
        struct : :class:`parmed.Structure`
            The input structure to generate from
        copy : bool, optional
            If True, assign from a *copy* of ``struct`` (this is a lot slower).
            Default is False

        Returns
        -------
        gmxtop : :class:`GromacsTopologyFile`
            The topology file defined by the given struct
        """
        from copy import copy as _copy
        from parmed.charmm import CharmmPsfFile
        gmxtop = cls()
        if copy:
            struct = _copy(struct)
        gmxtop.atoms = struct.atoms
        gmxtop.residues = struct.residues
        gmxtop.bonds = struct.bonds
        gmxtop.angles = struct.angles
        gmxtop.dihedrals = struct.dihedrals
        gmxtop.impropers = struct.impropers
        gmxtop.cmaps = struct.cmaps
        gmxtop.rb_torsions = struct.rb_torsions
        gmxtop.urey_bradleys = struct.urey_bradleys
        gmxtop.adjusts = struct.adjusts
        gmxtop.bond_types = struct.bond_types
        gmxtop.angle_types = struct.angle_types
        gmxtop.dihedral_types = struct.dihedral_types
        gmxtop.improper_types = struct.improper_types
        gmxtop.cmap_types = struct.cmap_types
        gmxtop.rb_torsion_types = struct.rb_torsion_types
        gmxtop.urey_bradley_types = struct.urey_bradley_types
        gmxtop.combining_rule = struct.combining_rule
        if (struct.trigonal_angles or
                struct.out_of_plane_bends or
                struct.pi_torsions or
                struct.stretch_bends or
                struct.torsion_torsions or
                struct.chiral_frames or
                struct.multipole_frames):
            raise TypeError('GromacsTopologyFile does not support Amoeba '
                            'potential terms')
        # Now check what the 1-4 scaling factors should be
        if hasattr(struct, 'defaults') and isinstance(struct.defaults,
                                                      _Defaults):
            gmxtop.defaults = struct.defaults
        else:
            scee_values = set()
            scnb_values = set()
            for dihedral in struct.dihedrals:
                if dihedral.type is None: continue
                if isinstance(dihedral.type, DihedralTypeList):
                    for dt in dihedral.type:
                        if dt.scee:
                            scee_values.add(dt.scee)
                        if dt.scnb:
                            scnb_values.add(dt.scnb)
                else:
                    if dihedral.type.scee:
                        scee_values.add(dihedral.type.scee)
                    if dihedral.type.scnb:
                        scnb_values.add(dihedral.type.scnb)
            if len(scee_values) > 1:
                raise GromacsError('Structure has mixed 1-4 scaling which is '
                                   'not supported by Gromacs')
            scee_values = list(scee_values)
            scnb_values = list(scnb_values)
            if len(scee_values) == 1:
                gmxtop.defaults.fudgeQQ = 1/scee_values[0]
            else:
                gmxtop.defaults.fudgeQQ = 1.0
            if len(scnb_values) == 1:
                gmxtop.defaults.fudgeLJ = 1/scnb_values[0]
            else:
                gmxtop.defaults.fudgeLJ = 1.0
        if gmxtop.combining_rule == 'geometric':
            gmxtop.defaults.comb_rule = 3

        return gmxtop

    #===================================================

    def write(self, dest, combine=None, parameters='inline'):
        """ Write a Gromacs Topology File from a Structure

        Parameters
        ----------
        dest : str or file-like
            The name of a file or a file object to write the Gromacs topology to
        combine : 'all', None, or list of iterables, optional
            If None, no molecules are combined into a single moleculetype. If
            'all', all molecules are combined into a single moleculetype.
            Otherwise, the list of molecule indices will control which atoms are
            combined into single moleculetype's. Default is None
        parameters : 'inline' or str or file-like object, optional
            This specifies where parameters should be printed. If 'inline'
            (default), the parameters are written on the same lines as the
            valence terms are defined on. Any other string is interpreted as a
            filename for an ITP that will be written to and then included at the
            top of `dest`. If it is a file-like object, parameters will be
            written there.  If parameters is the same as ``dest``, then the
            parameter types will be written to the same topologyfile.

        Raises
        ------
        ValueError if the same molecule number appears in multiple combine lists
        """
        import parmed.gromacs as gmx
        from parmed import __version__
        own_handle = False
        fname = ''
        params = ParameterSet.from_structure(self)
        if isinstance(dest, string_types):
            fname = '%s ' % dest
            dest = genopen(dest, 'w')
            own_handle = True
        elif not hasattr(dest, 'write'):
            raise TypeError('dest must be a file name or file-like object')

        # Determine where to write the parameters
        own_parfile_handle = False
        include_parfile = None
        if parameters == 'inline':
            parfile = dest
        elif isinstance(parameters, string_types):
            if parameters == fname.strip():
                parfile = dest
            else:
                own_parfile_handle = True
                parfile = genopen(parameters, 'w')
                include_parfile = parameters
        elif parameters is dest:
            # This is also OK -- we'll just write to the same file object
            pass
        elif hasattr(parameters, 'write'):
            parfile = parameters
        else:
            raise ValueError('parameters must be "inline", a file name, or '
                             'a file-like object')

        try:
            # Write the header
            now = datetime.now()
            dest.write('''\
;
;   File %swas generated
;   By user: %s (%d)
;   On host: %s
;   At date: %s
;
;   This is a standalone topology file
;
;   Created by:
;   ParmEd:       %s, VERSION %s
;   Executable:   %s
;   Library dir:  %s
;   Command line:
;     %s
;
''' % (fname, os.getlogin(), os.getuid(), os.uname()[1],
       now.strftime('%a. %B  %w %X %Y'), os.path.split(sys.argv[0])[1],
       __version__, os.path.split(sys.argv[0])[1], gmx.GROMACS_TOPDIR,
       ' '.join(sys.argv)))
            dest.write('\n[ defaults ]\n')
            dest.write('; nbfunc        comb-rule       gen-pairs       '
                        'fudgeLJ fudgeQQ\n')
            dest.write('%-15d %-15d %-15s %-7g %7g\n\n' %
                        (self.defaults.nbfunc, self.defaults.comb_rule,
                        self.defaults.gen_pairs, self.defaults.fudgeLJ,
                        self.defaults.fudgeQQ))
            if include_parfile is not None:
                dest.write('#include "%s"\n\n' % include_parfile)
            # Print all atom types
            parfile.write('[ atomtypes ]\n')
            if any(typ._bond_type is not None
                    for key, typ in iteritems(params.atom_types)):
                print_bond_types = True
            else:
                print_bond_types = False
            if all(typ.atomic_number != -1
                    for key, typ in iteritems(params.atom_types)):
                print_atnum = True
            else:
                print_atnum = False
            parfile.write('; name    ')
            if print_bond_types:
                parfile.write('bond_type ')
            if print_atnum:
                parfile.write('at.num    ')
            parfile.write('mass    charge ptype  sigma      espilon\n')
            econv = u.kilocalories.conversion_factor_to(u.kilojoules)
            for key, atom_type in iteritems(params.atom_types):
                parfile.write('%-7s ' % atom_type)
                if print_bond_types:
                    parfile.write('%-8s ' % atom_type.bond_type)
                if print_atnum:
                    parfile.write('%8d ' % atom_type.atomic_number)
                parfile.write('%10.5f  %10.6f  A %13.6g %13.6g\n' % (
                              0, atom_type.mass, atom_type.sigma/10,
                              atom_type.epsilon*econv))
            parfile.write('\n')
            # Print all parameter types unless we asked for inline
            if parameters != 'inline':
                if params.bond_types:
                    parfile.write('[ bondtypes ]\n')
                    parfile.write('; i    j  func       b0          kb\n')
                    used_keys = set()
                    conv = (u.kilocalorie/u.angstrom**2).conversion_factor_to(
                                u.kilojoule/u.nanometer**2) * 2
                    for key, param in iteritems(params.bond_types):
                        if key in used_keys: continue
                        used_keys.add(key)
                        used_keys.add(tuple(reversed(key)))
                        parfile.write('%-5s %-5s    1   %.5f   %f\n' % (key[0],
                                      key[1], param.req/10, param.k*conv))
                    parfile.write('\n')
                if params.angle_types:
                    parfile.write('[ angletypes ]\n')
                    parfile.write(';  i    j    k  func       th0       cth '
                                  '   rub         kub\n')
                    used_keys = set()
                    conv = (u.kilocalorie/u.radian**2).conversion_factor_to(
                                u.kilojoule/u.nanometer**2) * 2
                    bconv = (u.kilocalorie/u.angstrom**2).conversion_factor_to(
                                u.kilojoule/u.nanometer**2) * 2
                    for key, param in iteritems(params.angle_types):
                        if key in used_keys: continue
                        used_keys.add(key)
                        used_keys.add(tuple(reversed(key)))
                        part = '%-5s %-5s %-5s    %%d   %8.3f   %8.3f' % (
                                key[0], key[1], key[2], param.theteq,
                                param.k*conv)
                        if (key[0], key[2]) in params.urey_bradley_types:
                            ub = params.urey_bradley_types[(key[0], key[2])]
                            parfile.write(part % 5)
                            parfile.write('  %8.3f  %8.3f\n' % (ub.req/10,
                                          ub.k*bconv))
                        else:
                            parfile.write(part % 1)
                            parfile.write('\n')
                    parfile.write('\n')
                if params.dihedral_types:
                    parfile.write('[ dihedraltypes ]\n')
                    parfile.write(';i  j   k  l  func      phase      kd      '
                                  'pn\n')
                    used_keys = set()
                    conv = u.kilocalories.conversion_factor_to(u.kilojoules)
                    fmt = '%-6s %-6s %-6s  %d   %.2f   %.6f   %d\n'
                    for key, param in iteritems(params.dihedral_types):
                        if key in used_keys: continue
                        used_keys.add(key)
                        used_keys.add(tuple(reversed(key)))
                        if isinstance(param, DihedralTypeList):
                            funct = 9
                            for dt in param:
                                parfile.write(fmt % (key[0], key[1], key[2],
                                              key[3], funct, dt.phase,
                                              dt.phi_k*conv, int(dt.per)))
                        else:
                            funct = 1
                            parfile.write(fmt % (key[0], key[1], key[2], key[3],
                                          funct, param.phase, param.phi_k*conv,
                                          int(param.per)))
                    parfile.write('\n')
                if params.improper_periodic_types:
                    parfile.write('[ dihedraltypes ]\n')
                    parfile.write(';i  j   k  l  func      phase      kd      '
                                  'pn\n')
                    used_keys = set()
                    conv = u.kilojoules.conversion_factor_to(u.kilocalories)
                    fmt = '%-6s %-6s %-6s  %d   %.2f   %.6f   %d\n'
                    for key, param in iteritems(params.improper_periodic_types):
                        if key in used_keys: continue
                        used_keys.add(key)
                        used_keys.add(tuple(reversed(key)))
                        parfile.write(fmt % (key[0], key[1], key[2], key[3],
                                      4, param.phase, param.phi_k*conv,
                                      int(param.per)))
                    parfile.write('\n')
                if params.improper_types:
                    # BUGBUG -- The ordering is borked here because that made it
                    # simpler for me to work with back when I wrote the CHARMM
                    # parsers. This needs to be fixed now and handled correctly.
                    parfile.write('[ dihedraltypes ]\n')
                    parfile.write('; i  j       k       l       func     q0    '
                                  'cq\n')
                    fmt = '%-6s %-6s %-6s %-6s    %d   %.4f   %.4f\n'
                    conv = u.kilocalories.conversion_factor_to(u.kilojoules)*2
                    for key, param in iteritems(params.improper_types):
                        parfile.write(fmt % (key[0], key[1], key[2], key[3],
                                      2, param.psi_eq, param.psi_k*conv))
                    parfile.write('\n')
            # CMAP grids are never printed inline, so if we have them, we need
            # to write a dedicated section for them
            if params.cmap_types:
                    parfile.write('[ cmaptypes ]\n\n')
                    used_keys = set()
                    conv = u.kilocalories.conversion_factor_to(u.kilojoules)
                    for key, param in iteritems(params.cmap_types):
                        if key in used_keys: continue
                        used_keys.add(key)
                        used_keys.add(tuple(reversed(key)))
                        parfile.write('%-6s %-6s %-6s %-6s %-6s   1   '
                                      '%4d %4d' % (key[0], key[1], key[2],
                                      key[3], key[4], param.resolution,
                                      param.resolution))
                        res2 = param.resolution * param.resolution
                        for i in range(0, res2, 10):
                            parfile.write('\\\n')
                            end = min(i+10, res2)
                            parfile.write(' '.join(str(param.grid[j]*conv)
                                          for j in range(i, end)))
                        parfile.write('\n\n')
            if combine is None:
                molecules = self.split()
                sysnum = 1
                names = []
                nameset = set()
                for molecule, num in molecules:
                    if len(molecule.residues) == 1:
                        title = molecule.residues[0].name
                        if title in nameset:
                            orig = title
                            sfx = 2
                            while title in nameset:
                                title = '%s%d' % (orig, sfx)
                                sfx += 1
                    else:
                        title = 'system%d' % sysnum
                        sysnum += 1
                    names.append(title)
                    nameset.add(title)
                    GromacsTopologyFile._write_molecule(molecule, dest, title,
                                        params, parameters == 'inline')
                # System
                dest.write('[ system ]\n; Name\n')
                if self.title:
                    dest.write(self.title)
                else:
                    dest.write('Generic title')
                dest.write('\n\n')
                # Molecules
                dest.write('[ molecules ]\n; Compound       #mols\n')
                for i, (molecule, num) in enumerate(molecules):
                    dest.write('%-15s %6d\n' % (names[i], num))
            elif isinstance(combine, string_types) and combine.lower() == 'all':
                GromacsTopologyFile._write_molecule(self, dest, 'system',
                                    params, parameters == 'inline')
            else:
                raise NotImplementedError('Specialized molecule splitting is '
                                          'not yet supported')
        finally:
            if own_handle:
                dest.close()
            if own_parfile_handle:
                parfile.close()

    #===================================================

    @staticmethod
    def _write_molecule(struct, dest, title, params, writeparams):
        dest.write('\n[ moleculetype ]\n; Name            nrexcl\n')
        dest.write('%s          %d\n\n' % (title, struct.nrexcl))
        dest.write('[ atoms ]\n')
        dest.write(';   nr       type  resnr residue  atom   cgnr    '
                   'charge       mass  typeB    chargeB      massB\n')
        runchg = 0
        for residue in struct.residues:
            dest.write('; residue %4d %s rtp %s q %.1f\n' %
                       (residue.idx+1, residue.name, residue.name,
                        sum(a.charge for a in residue)))
            for atom in residue:
                runchg += atom.charge
                dest.write('%5d %10s %6d %6s %6s %6d %10.6f %10.4f   ; '
                           'qtot %.4f\n' % (atom.idx+1, atom.type,
                            residue.idx+1, residue.name, atom.name,
                            atom.idx+1, atom.charge, atom.mass, runchg))
        dest.write('\n')
        # Do valence terms now
        EPs = [a for a in struct.atoms if isinstance(a, ExtraPoint)]
        settle = False
        if len(struct.atoms) - len(EPs) == 3:
            try:
                oxy, = (a for a in struct.atoms if a.atomic_number == 8)
                hyd1, hyd2 = (a for a in struct.atoms if a.atomic_number == 1)
                settle = True
            except ValueError:
                pass
        if struct.bonds:
            conv = (u.kilocalorie_per_mole/u.angstrom**2).conversion_factor_to(
                    u.kilojoule_per_mole/u.nanometer**2)*2
            if settle:
                dest.write('#ifdef FLEXIBLE\n\n')
            dest.write('[ bonds ]\n')
            dest.write(';%6s %6s %5s %10s %10s %10s %10s\n' % ('ai', 'aj',
                       'funct', 'c0', 'c1', 'c2', 'c3'))
            for bond in struct.bonds:
                if (isinstance(bond.atom1, ExtraPoint) or
                        isinstance(bond.atom2, ExtraPoint)):
                    continue
                dest.write('%7d %6d %5d' % (bond.atom1.idx+1,
                           bond.atom2.idx+1, bond.funct))
                if bond.type is None:
                    dest.write('\n')
                    continue
                key = (_gettype(bond.atom1), _gettype(bond.atom2))
                if writeparams or key not in params.bond_types or \
                        bond.type != params.bond_types[key]:
                    dest.write('   %.5f %f' % (bond.type.req/10,
                                               bond.type.k*conv))
                dest.write('\n')
            dest.write('\n')
        # Do the pair-exceptions
        if struct.adjusts:
            dest.write('[ pairs ]\n')
            dest.write(';%6s %6s %5s %10s %10s %10s %10s\n' % ('ai', 'aj',
                       'funct', 'c0', 'c1', 'c2', 'c3'))
            for adjust in struct.adjusts:
                dest.write('%7d %6d %5d\n' % (adjust.atom1.idx+1,
                           adjust.atom2.idx+1, adjust.funct))
            dest.write('\n')
        elif struct.dihedrals:
            dest.write('[ pairs ]\n')
            dest.write(';%6s %6s %5s %10s %10s %10s %10s\n' % ('ai', 'aj',
                       'funct', 'c0', 'c1', 'c2', 'c3'))
            # Get the 1-4 pairs from the dihedral list
            for dihed in struct.dihedrals:
                if dihed.ignore_end or dihed.improper: continue
                a1, a2 = dihed.atom1, dihed.atom4
                if a1 in a2.bond_partners or a1 in a2.angle_partners:
                    continue
                dest.write('%7d %6d %5d\n' % (a1.idx+1, a2.idx+1, 1))
            dest.write('\n')
        # Angles
        if struct.angles:
            conv = (u.kilocalorie_per_mole/u.radian**2).conversion_factor_to(
                        u.kilojoule_per_mole/u.radian**2)*2
            conv2 = (u.kilocalorie_per_mole/u.angstrom**2).conversion_factor_to(
                    u.kilojoule_per_mole/u.nanometer**2)*2
            dest.write('[ angles ]\n')
            dest.write(';%6s %6s %6s %5s %10s %10s %10s %10s\n' %
                       ('ai', 'aj', 'ak', 'funct', 'c0', 'c1', 'c2', 'c3'))
            for angle in struct.angles:
                dest.write('%7d %6d %6d %5d' % (angle.atom1.idx+1,
                           angle.atom2.idx+1, angle.atom3.idx+1,
                           angle.funct))
                if angle.type is None:
                    dest.write('\n')
                    continue
                key = (_gettype(angle.atom1), _gettype(angle.atom2),
                       _gettype(angle.atom3))
                param_equal = (key in params.angle_types and
                                    params.angle_types[key] == angle.type)
                if angle.funct == 5:
                    # Find the Urey-Bradley term, if it exists
                    for ub in struct.urey_bradleys:
                        if angle.atom1 in ub and angle.atom3 in ub:
                            ubtype = ub.type
                            break
                    else:
                        ubtype = NoUreyBradley
                    ubkey = (key[0], key[2])
                    param_equal = param_equal and (
                            ubkey in params.urey_bradley_types and
                            ubtype == params.urey_bradley_types[ubkey])
                if writeparams or not param_equal:
                    dest.write('   %.5f %f' % (angle.type.theteq,
                                               angle.type.k*conv))
                    if angle.funct == 5:
                        dest.write(' %.5f %f' % (ubtype.req/10, ubtype.k*conv2))
                dest.write('\n')
            dest.write('\n')
        # Dihedrals
        if struct.dihedrals:
            dest.write('[ dihedrals ]\n')
            dest.write((';%6s %6s %6s %6s %5s'+' %10s'*6) % ('ai', 'aj',
                       'ak', 'al', 'funct', 'c0', 'c1', 'c2', 'c3',
                       'c4', 'c5'))
            dest.write('\n')
            conv = u.kilocalories.conversion_factor_to(u.kilojoules)
            for dihed in struct.dihedrals:
                dest.write('%7d %6d %6d %6d %5d' % (dihed.atom1.idx+1,
                           dihed.atom2.idx+1, dihed.atom3.idx+1,
                           dihed.atom4.idx+1, dihed.funct))
                if dihed.type is None:
                    dest.write('\n')
                    continue
                if dihed.improper:
                    typedict = params.improper_periodic_types
                else:
                    typedict = params.dihedral_types
                key = (_gettype(dihed.atom1), _gettype(dihed.atom2),
                        _gettype(dihed.atom3), _gettype(dihed.atom4))
                if writeparams or key not in typedict or \
                        _diff_diheds(dihed.type, typedict[key]):
                    if isinstance(dihed.type, DihedralTypeList):
                        dest.write('  %.5f  %.5f  %d\n' % (dihed.type[0].phase,
                            dihed.type[0].phi_k*conv, int(dihed.type[0].per)))
                        for dt in dihed.type[1:]:
                            dest.write('%7d %6d %6d %6d %5d  %.5f  %.5f  %d\n' %
                                    (dihed.atom1.idx+1, dihed.atom2.idx+1,
                                     dihed.atom3.idx+1, dihed.atom4.idx+1,
                                     dihed.funct, dt.phase, dt.phi_k*conv,
                                     int(dt.per)))
                    else:
                        dest.write('  %.5f  %.5f  %d\n' % (dihed.type.phase,
                            dihed.type.phi_k*conv, int(dihed.type.per)))
            dest.write('\n')
        # RB-torsions
        if struct.rb_torsions:
            dest.write('[ dihedrals ]\n')
            dest.write((';%6s %6s %6s %6s %5s'+' %10s'*6) % ('ai', 'aj',
                       'ak', 'al', 'funct', 'c0', 'c1', 'c2', 'c3',
                       'c4', 'c5'))
            dest.write('\n')
            conv = u.kilocalories.conversion_factor_to(u.kilojoules)
            paramfmt = '  %12.5f  %12.5f  %12.5f  %12.5f  %12.5f  %12.5f'
            for dihed in struct.rb_torsions:
                dest.write('%7d %6d %6d %6d %5d' % (dihed.atom1.idx+1,
                           dihed.atom2.idx+1, dihed.atom3.idx+1,
                           dihed.atom4.idx+1, dihed.funct))
                if dihed.type is None:
                    dest.write('\n')
                    continue
                key = (_gettype(dihed.atom1), _gettype(dihed.atom2),
                        _gettype(dihed.atom3), _gettype(dihed.atom4))
                if writeparams or key not in params.rb_torsion_types or \
                        params.rb_torsion_types[key] != dihed.type:
                    dest.write(paramfmt % (dihed.type.c0*conv,
                                           dihed.type.c1.conv,
                                           dihed.type.c2.conv,
                                           dihed.type.c3.conv,
                                           dihed.type.c4.conv,
                                           dihed.type.c5.conv))
                    dest.write('\n')
            dest.write('\n')
        # Impropers
        if struct.impropers:
            dest.write('[ dihedrals ]\n')
            dest.write((';%6s %6s %6s %6s %5s'+' %10s'*4) % ('ai', 'aj',
                       'ak', 'al', 'funct', 'c0', 'c1', 'c2', 'c3'))
            dest.write('\n')
            conv = u.kilocalories.conversion_factor_to(u.kilojoules) * 2
            for dihed in struct.impropers:
                dest.write('%7d %6d %6d %6d %5d' % (dihed.atom1.idx+1,
                           dihed.atom2.idx+1, dihed.atom3.idx+1,
                           dihed.atom4.idx+1, dihed.funct))
                if dihed.type is None:
                    dest.write('\n')
                    continue
                # BUGBUG: We always write improper types since we don't
                # currently store the correct ordering of the types in the
                # improper section
                dest.write('  %12.5f  %12.5f\n' % (dihed.type.psi_eq,
                                                   dihed.type.psi_k*conv))
            dest.write('\n')
        # Cmaps
        if struct.cmaps:
            dest.write('[ cmap ]\n')
            dest.write(';%6s %6s %6s %6s %6s %5s\n' % ('ai', 'aj', 'ak',
                       'al', 'am', 'funct'))
            for cmap in struct.cmaps:
                dest.write('%7d %6d %6d %6d %6d %5d\n' % (cmap.atom1.idx+1,
                           cmap.atom2.idx+1, cmap.atom3.idx+1,
                           cmap.atom4.idx+1, cmap.atom5.idx+1, cmap.funct))
        # See if this is a solvent molecule with 3 or fewer particles that can
        # be SETTLEd
        if settle:
            dest.write('\n#else\n\n')
            dest.write('[ settles ]\n')
            dest.write('; i     funct   doh     dhh\n')
            for b in oxy.bonds:
                if hyd1 in b:
                    if b.type is None:
                        # Use default values for TIPnP
                        doh = 0.09572
                    else:
                        doh = b.type.req / 10
                    break
            for b in hyd1.bonds:
                if hyd2 in b:
                    if b.type is None:
                        # Use default values for TIPnP
                        dhh = 0.15139
                    else:
                        dhh = b.type.req / 10
                    break
            else:
                for a in oxy.angles:
                    if hyd1 in a and hyd2 in a:
                        theteq = a.type.theteq * DEG_TO_RAD
                        dhh = math.sqrt(2*doh*doh - 2*doh*doh*math.cos(theteq))
                        break
                else:
                    raise GromacsError('Cannot determine SETTLE geometry')
            dest.write('1     1   %.5f   %.5f\n\n#endif\n\n' % (doh, dhh))
        # Virtual sites
        if EPs:
            ftypes = set(type(a.frame_type) for a in EPs)
            for ftype in ftypes:
                if ftype is TwoParticleExtraPointFrame:
                    dest.write('[ virtual_sites2 ]\n')
                    dest.write('; Site  from    funct  a\n')
                    for EP in EPs:
                        if not isinstance(EP.frame_type, ftype): continue
                        a1, a2 = EP.frame_type.get_atoms()
                        dest.write('%-5d %-4d %-4d %-4d   %.6f\n' %
                                   (EP.idx+1, a1.idx+1, a2.idx+1, 1,
                                    EP.get_weights()[0]))
                    dest.write('\n')
                elif ftype in (ThreeParticleExtraPointFrame,
                               OutOfPlaneExtraPointFrame):
                    dest.write('[ virtual_sites3 ]\n')
                    dest.write('; Site  from                   funct\n')
                    for EP in EPs:
                        if isinstance(EP.frame_type,
                                ThreeParticleExtraPointFrame):
                            a1, a2, a3 = EP.frame_type.get_atoms()
                            junk, w1, w2 = EP.frame_type.get_weights()
                            dest.write('%-5d %-4d %-4d %-4d %-4d   %.6f  %.6f\n'
                                    % (EP.idx+1, a1.idx+1, a2.idx+1, a3.idx+1,
                                       1, w1, w2))
                        elif isinstance(EP.frame_type,
                                OutOfPlaneExtraPointFrame):
                            a1, a2, a3 = EP.frame_type.get_atoms()
                            w1, w2, w3 = EP.frame_type.get_weights()
                            dest.write('%-5d %-4d %-4d %-4d %-4d   %.6f  %.6f  '
                                       '%.6f\n' % (EP.idx+1, a1.idx+1, a2.idx+1,
                                       a3.idx+1, w1, w2, w3))
                    dest.write('\n')
        # Do we need to list exclusions for systems with EPs?
        if EPs or settle:
            dest.write('[ exclusions ]\n')
            for i, atom in enumerate(struct.atoms):
                dest.write('%d' % (i+1))
                for a in atom.bond_partners:
                    dest.write('  %d' % (a.idx+1))
                for a in atom.angle_partners:
                    dest.write('  %d' % (a.idx+1))
                dest.write('\n')
            dest.write('\n')

def _any_atoms_farther_than(structure, limit=3):
    """
    This function checks to see if there are any atom pairs farther away in the
    bond graph than the desired limit

    Parameters
    ----------
    structure : :class:`Structure`
        The structure to search through
    limit : int, optional
        The most number of bonds away to check for. Default is 3

    Returns
    -------
    within : bool
        True if any atoms are *more* than ``limit`` bonds away from any other
        atom
    """
    import sys
    if len(structure.atoms) <= limit + 1: return False
    sys.setrecursionlimit(max(sys.getrecursionlimit(), limit+1))
    for atom in structure.atoms:
        for atom in structure.atoms: atom.marked = limit + 1
        _mark_graph(atom, 0)
        if any((atom.marked > limit for atom in structure.atoms)):
            return True
    return False

def _mark_graph(atom, num):
    """ Marks all atoms in the graph listing the minimum number of bonds each
    atom is away from the current atom

    Parameters
    ----------
    atom : :class:`Atom`
        The current atom to evaluate in the bond graph
    num : int
        The current depth in our search
    limit : int
        The maximum depth we want to search
    """
    atom.marked = num
    for a in atom.bond_partners:
        if a.marked <= num: continue
        _mark_graph(a, num+1)

def _diff_diheds(dt1, dt2):
    """ Determine if 2 dihedrals are *really* different. dt1 can either be a
    DihedralType or a DihedralTypeList or dt1 can be a DihedralType and dt2 can
    be a DihedralTypeList.  This returns True if dt1 == dt2 *or* dt1 is equal to
    the only element of dt2
    """
    if dt1 == dt2:
        return False
    if isinstance(dt2, DihedralTypeList) and isinstance(dt1, DihedralType):
        if len(dt2) == 1 and dt2[0] == dt1: return False
    return True

def _gettype(atom):
    if atom.atom_type is not None:
        return atom.atom_type.bond_type
    return atom.type