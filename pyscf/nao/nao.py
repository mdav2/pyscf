from __future__ import print_function, division
import sys, numpy as np

from pyscf.nao.m_color import color as bc
from pyscf.nao.m_system_vars_dos import system_vars_dos, system_vars_pdos
from pyscf.nao.m_siesta2blanko_csr import _siesta2blanko_csr
from pyscf.nao.m_siesta2blanko_denvec import _siesta2blanko_denvec
from pyscf.nao.m_siesta_ion_add_sp2 import _siesta_ion_add_sp2
from pyscf.nao.m_ao_log import ao_log_c
from scipy.spatial.distance import cdist
    
#
#
#
def get_orb2m(sv):
  orb2m = np.empty(sv.norbs, dtype='int64')
  orb = 0
  for atom,sp in enumerate(sv.atom2sp):
    for mu,j in enumerate(sv.sp_mu2j[sp]):
      for m in range(-j,j+1): orb2m[orb],orb = m,orb+1
  return orb2m

#
#
#
def get_orb2j(sv):
  orb2j = np.empty(sv.norbs, dtype='int64')
  orb = 0
  for atom,sp in enumerate(sv.atom2sp):
    for mu,j in enumerate(sv.sp_mu2j[sp]):
      for m in range(-j,j+1): orb2j[orb],orb = j,orb+1
  return orb2j

#
#
#
def overlap_check(sv, tol=1e-5, **kvargs):
  over = sv.overlap_coo(**kvargs).tocsr()
  diff = (sv.hsx.s4_csr-over).sum()
  summ = (sv.hsx.s4_csr+over).sum()
  ac = diff/summ<tol
  if not ac: print(diff, summ)
  return ac

#
#
#
class nao():

  def __init__(self, **kw):
    """  Constructor of NAO class """

    import scipy
    if int(scipy.__version__[0])>0: 
      scipy_ver_def = 1;
    else:
      scipy_ver_def = 0
    self.scipy_ver = kw['scipy_ver'] if 'scipy_ver' in kw else scipy_ver_def

    try:
      import numba
      use_numba_def = True
    except:
      use_numba_def = False
    self.use_numba = kw['use_numba'] if 'use_numba' in kw else use_numba_def

    self.numba_parallel = kw["numba_parallel"] if "numba_parallel" in kw else True 
    
    self.verbosity = kw['verbosity'] if 'verbosity' in kw else 0
    self.verbose = self.verbosity

    if 'gto' in kw:
      self.init_gto(**kw)
      self.init_libnao_orbs()
    elif 'xyz_list' in kw:
      self.init_xyz_list(**kw)
    elif 'label' in kw:
      self.init_label(**kw)
      self.init_libnao_orbs()
    elif 'gpaw' in kw:
      self.init_gpaw(**kw)
      self.init_libnao_orbs()
    elif 'openmx' in kw:
      self.init_openmx(**kw)
      #self.init_libnao_orbs()
    elif 'fireball' in kw:
      self.init_fireball(**kw)
    else:
      raise RuntimeError('unknown init method')
    
    #print(kw)
    #print(dir(kw))

  #
  #
  #
  def init_gto(self, **kw):
    """Interpret previous pySCF calculation"""
    from pyscf.lib import logger

    gto = kw['gto']
    self.stdout = sys.stdout
    self.symmetry = False
    self.symmetry_subgroup = None
    self.cart = False
    self._nelectron = gto.nelectron
    self._built = True
    self.max_memory = 20000

    self.spin = gto.spin
    #print(__name__, 'dir(gto)', dir(gto), gto.nelec)
    self.nspin = 1 if gto.spin==0 else 2 # this can be wrong and has to be redetermined at in the mean-field class (mf)
    self.label = kw['label'] if 'label' in kw else 'pyscf'
    self.mol=gto # Only some data must be copied, not the whole object. Otherwise, an eventual deepcopy(...) may fail.
    self.natm=self.natoms = gto.natm
    a2s = [gto.atom_symbol(ia) for ia in range(gto.natm) ]
    self.sp2symbol = sorted(list(set(a2s)))
    self.nspecies = len(self.sp2symbol)
    self.atom2sp = np.empty((gto.natm), dtype=np.int64)
    for ia,sym in enumerate(a2s): self.atom2sp[ia] = self.sp2symbol.index(sym)

    self.sp2charge = [-999]*self.nspecies
    for ia,sp in enumerate(self.atom2sp): self.sp2charge[sp]=gto.atom_charge(ia)
    self.ao_log = ao_log_c().init_ao_log_gto_suggest_mesh(nao=self, **kw)
    self.atom2coord = np.zeros((self.natm, 3))
    for ia,coord in enumerate(gto.atom_coords()): self.atom2coord[ia,:]=coord # must be in Bohr already?
    self.atom2s = np.zeros((self.natm+1), dtype=np.int64)
    for atom,sp in enumerate(self.atom2sp): self.atom2s[atom+1]=self.atom2s[atom]+self.ao_log.sp2norbs[sp]
    self.norbs = self.norbs_sc = self.atom2s[-1]
    self.ucell = 30.0*np.eye(3)
    self.atom2mu_s = np.zeros((self.natm+1), dtype=np.int64)
    for atom,sp in enumerate(self.atom2sp): self.atom2mu_s[atom+1]=self.atom2mu_s[atom]+self.ao_log.sp2nmult[sp]
    self._atom = gto._atom
    self.basis = gto.basis
    ### implement when needed  self.init_libnao()
    self.nbas = self.atom2mu_s[-1] # total number of radial orbitals
    self.mu2orb_s = np.zeros((self.nbas+1), dtype=np.int64)
    for sp,mu_s in zip(self.atom2sp,self.atom2mu_s):
      for mu,j in enumerate(self.ao_log.sp_mu2j[sp]): self.mu2orb_s[mu_s+mu+1] = self.mu2orb_s[mu_s+mu] + 2*j+1
    self.sp_mu2j = self.ao_log.sp_mu2j
    self.nkpoints = 1
    return self

  #
  #
  #
  def init_xyz_list(self, **kw):
    """ This is simple constructor which only initializes geometry info """
    from pyscf.lib import logger
    from pyscf.lib.parameters import ELEMENTS as chemical_symbols
    self.verbose = logger.NOTE  # To be similar to Mole object...
    self.stdout = sys.stdout
    self.symmetry = False
    self.symmetry_subgroup = None
    self.cart = False

    self.label = kw['label'] if 'label' in kw else 'pyscf'
    atom = kw['xyz_list']
    atom2charge = [atm[0] for atm in atom]
    self.atom2coord = np.array([atm[1] for atm in atom])
    self.sp2charge = list(set(atom2charge))
    self.sp2symbol = [chemical_symbols[z] for z in self.sp2charge]
    self.atom2sp = [self.sp2charge.index(charge) for charge in atom2charge]
    self.natm=self.natoms=len(self.atom2sp)
    self.atom2s = None
    self.nspin = 1
    self.nbas  = self.natm
    self.state = 'should be useful for something'
    return self

  #
  #
  #
  def init_fireball(self, **kw):
    from pyscf.nao.m_fireball_import import fireball_import
    from timeit import default_timer as timer
    """
      Initialise system var using only the fireball files (standard output in particular is needed)
      System variables:
      -----------------
        chdir (string): calculation directory
    """
    fireball_import(self, **kw)
    return self

  #
  #
  #
  def init_label(self, **kw):
    from pyscf.nao.m_siesta_xml import siesta_xml
    from pyscf.nao.m_siesta_wfsx import siesta_wfsx_c
    from pyscf.nao.m_siesta_ion_xml import siesta_ion_xml
    from pyscf.nao.m_siesta_hsx import siesta_hsx_c
    from timeit import default_timer as timer
    """
      Initialise system var using only the siesta files (siesta.xml in particular is needed)

      System variables:
      -----------------
        label (string): calculation label
        chdir (string): calculation directory
        xml_dict (dict): information extracted from the xml siesta output, see m_siesta_xml
        wfsx: class use to extract the information about wavefunctions, see m_siesta_wfsx
        hsx: class to store a sparse representation of hamiltonian and overlap, see m_siesta_hsx
        norbs_sc (integer): number of orbital
        ucell (array, float): unit cell
        sp2ion (list): species to ions, list of the species associated to the information from the ion files, see m_siesta_ion_xml
        ao_log: Atomic orbital on an logarithmic grid, see m_ao_log
        atom2coord (array, float): array containing the coordinates of each atom.
        natm, natoms (integer): number of atoms
        norbs (integer): number of orbitals
        nspin (integer): number of spin
        nkpoints (integer): number of kpoints
        fermi_energy (float): Fermi energy
        atom2sp (list): atom to specie, list associating the atoms to their specie number
        atom2s: atom -> first atomic orbital in a global orbital counting
        atom2mu_s: atom -> first multiplett (radial orbital) in a global counting of radial orbitals
        sp2symbol (list): list associating the species to their symbol
        sp2charge (list): list associating the species to their charge
        state (string): this is an internal information on the current status of the class
    """

    #label='siesta', cd='.', verbose=0, **kvargs

    self.label = label = kw['label'] if 'label' in kw else 'siesta'
    self.cd = cd = kw['cd'] if 'cd' in kw else '.'
    self.xml_dict = siesta_xml(cd+'/'+self.label+'.xml')
    self.wfsx = siesta_wfsx_c(**kw)
    self.hsx = siesta_hsx_c(fname=cd+'/'+self.label+'.HSX', **kw)
    self.norbs_sc = self.wfsx.norbs if self.hsx.orb_sc2orb_uc is None else len(self.hsx.orb_sc2orb_uc)
    self.ucell = self.xml_dict["ucell"]
    ##### The parameters as fields     
    self.sp2ion = []
    for sp in self.wfsx.sp2strspecie: self.sp2ion.append(siesta_ion_xml(cd+'/'+sp+'.ion.xml'))

    _siesta_ion_add_sp2(self, self.sp2ion)
    self.ao_log = ao_log_c().init_ao_log_ion(self.sp2ion, **kw)

    self.atom2coord = self.xml_dict['atom2coord']
    self.natm=self.natoms=len(self.xml_dict['atom2sp'])
    self.norbs  = self.wfsx.norbs 
    self.nspin  = self.wfsx.nspin
    self.nkpoints  = self.wfsx.nkpoints
    self.fermi_energy = self.xml_dict['fermi_energy']

    strspecie2sp = {}
    # initialise a dictionary with species string as key
    # associated to the specie number
    for sp,strsp in enumerate(self.wfsx.sp2strspecie): strspecie2sp[strsp] = sp
    
    # list of atoms associated to them specie number
    self.atom2sp = np.empty((self.natm), dtype=np.int64)
    for o,atom in enumerate(self.wfsx.orb2atm):
      self.atom2sp[atom-1] = strspecie2sp[self.wfsx.orb2strspecie[o]]

    self.atom2s = np.zeros((self.natm+1), dtype=np.int64)
    for atom,sp in enumerate(self.atom2sp):
        self.atom2s[atom+1]=self.atom2s[atom]+self.ao_log.sp2norbs[sp]

    # atom2mu_s list of atom associated to them multipletts (radial orbitals)
    self.atom2mu_s = np.zeros((self.natm+1), dtype=np.int64)
    for atom,sp in enumerate(self.atom2sp):
        self.atom2mu_s[atom+1]=self.atom2mu_s[atom]+self.ao_log.sp2nmult[sp]
    
    orb2m = self.get_orb2m()
    _siesta2blanko_csr(orb2m, self.hsx.s4_csr, self.hsx.orb_sc2orb_uc)

    for s in range(self.nspin):
      _siesta2blanko_csr(orb2m, self.hsx.spin2h4_csr[s], self.hsx.orb_sc2orb_uc)
    
    #t1 = timer()
    for k in range(self.nkpoints):
      for s in range(self.nspin):
        for n in range(self.norbs):
          _siesta2blanko_denvec(orb2m, self.wfsx.x[k,s,n,:,:])
    #t2 = timer(); print(t2-t1, 'rsh wfsx'); t1 = timer()

    
    self.sp2symbol = [str(ion['symbol'].replace(' ', '')) for ion in self.sp2ion]
    self.sp2charge = self.ao_log.sp2charge
    self.state = 'should be useful for something'

    # Trying to be similar to mole object from pySCF 
    self._xc_code   = 'LDA,PZ' # estimate how ? 
    self._nelectron = self.hsx.nelec
    self.cart = False
    self.spin = self.nspin-1
    self.stdout = sys.stdout
    self.symmetry = False
    self.symmetry_subgroup = None
    self._built = True 
    self.max_memory = 20000
    self.incore_anyway = False
    self.nbas = self.atom2mu_s[-1] # total number of radial orbitals
    self.mu2orb_s = np.zeros((self.nbas+1), dtype=np.int64)
    for sp,mu_s in zip(self.atom2sp,self.atom2mu_s):
      for mu,j in enumerate(self.ao_log.sp_mu2j[sp]): self.mu2orb_s[mu_s+mu+1] = self.mu2orb_s[mu_s+mu] + 2*j+1
        
    self._atom = [(self.sp2symbol[sp], list(self.atom2coord[ia,:])) for ia,sp in enumerate(self.atom2sp)]
    return self

  def init_gpaw(self, **kw):
    """ Use the data from a GPAW LCAO calculations as input to initialize system variables. """
    try:
        import ase
        import gpaw
    except:
        raise ValueError("ASE and GPAW must be installed for using system_vars_gpaw")
    from pyscf.nao.m_system_vars_gpaw import system_vars_gpaw
    return system_vars_gpaw(self, **kw)

  #
  #
  #
  def init_openmx(self, **kw):
    from pyscf.nao.m_openmx_import_scfout import openmx_import_scfout
    from timeit import default_timer as timer
    """
      Initialise system var using only the OpenMX output (label.scfout in particular is needed)

      System variables:
      -----------------
        label (string): calculation label
        chdir (string): calculation directory
        xml_dict (dict): information extracted from the xml siesta output, see m_siesta_xml
        wfsx: class use to extract the information about wavefunctions, see m_siesta_wfsx
        hsx: class to store a sparse representation of hamiltonian and overlap, see m_siesta_hsx
        norbs_sc (integer): number of orbital
        ucell (array, float): unit cell
        sp2ion (list): species to ions, list of the species associated to the information from the pao files
        ao_log: Atomic orbital on an logarithmic grid, see m_ao_log
        atom2coord (array, float): array containing the coordinates of each atom.
        natm, natoms (integer): number of atoms
        norbs (integer): number of orbitals
        nspin (integer): number of spin
        nkpoints (integer): number of kpoints
        fermi_energy (float): Fermi energy
        atom2sp (list): atom to specie, list associating the atoms to their specie number
        atom2s: atom -> first atomic orbital in a global orbital counting
        atom2mu_s: atom -> first multiplett (radial orbital) in a global counting of radial orbitals
        sp2symbol (list): list soociating the species to their symbol
        sp2charge (list): list associating the species to their charge
        state (string): this is an internal information on the current status of the class
    """
    #label='openmx', cd='.', **kvargs
    openmx_import_scfout(self, **kw)
    self.state = 'must be useful for something already'
    return self

  # More functions for similarity with Mole
  def atom_symbol(self, ia): return self.sp2symbol[self.atom2sp[ia]]
  def atom_charge(self, ia): return self.sp2charge[self.atom2sp[ia]]
  def atom_charges(self): return np.array([self.sp2charge[sp] for sp in self.atom2sp], dtype='int64')
  def atom_coord(self, ia): return self.atom2coord[ia,:]
  def atom_coords(self): return self.atom2coord
  def nao_nr(self): return self.norbs
  def atom_nelec_core(self, ia): return self.sp2charge[self.atom2sp[ia]]-self.ao_log.sp2valence[self.atom2sp[ia]]
  def ao_loc_nr(self): return self.mu2orb_s[0:self.natm]

  # More functions for convenience (see PDoS)
  def get_orb2j(self): return get_orb2j(self)
  def get_orb2m(self): return get_orb2m(self)

  def overlap_coo(self, **kw):   # Compute overlap matrix for the molecule
    from pyscf.nao.m_overlap_coo import overlap_coo
    return overlap_coo(self, **kw)

  def overlap_lil(self, **kw):   # Compute overlap matrix in list of lists format
    from pyscf.nao.m_overlap_lil import overlap_lil
    return overlap_lil(self, **kw)

  def laplace_coo(self):   # Compute matrix of Laplace brakets for the whole molecule
    from pyscf.nao.m_overlap_coo import overlap_coo
    from pyscf.nao.m_laplace_am import laplace_am
    return overlap_coo(self, funct=laplace_am)
  
  def vnucele_coo_coulomb(self, **kw): # Compute matrix elements of attraction by Coulomb forces from point nuclei
    from pyscf.nao.m_vnucele_coo_coulomb import vnucele_coo_coulomb
    return vnucele_coo_coulomb(self, **kw)

  def dipole_coo(self, **kw):   # Compute dipole matrix elements for the given system
    from pyscf.nao.m_dipole_coo import dipole_coo
    return dipole_coo(self, **kw)
  
  def overlap_check(self, tol=1e-5, **kw): # Works only after init_siesta_xml(), extend ?
    return overlap_check(self, tol=1e-5, **kw)

  def energy_nuc(self, charges=None, coords=None):
    """ Potential energy of electrostatic repulsion of point nuclei """
    from scipy.spatial.distance import cdist
    chrg = self.atom_charges() if charges is None else charges
    crds = self.atom_coords() if coords is None else coords
    identity = np.identity(len(chrg))
    return ((chrg[:,None]*chrg[None,:])*(1.0/(cdist(crds, crds)+identity)-identity)).sum()*0.5

  def build_3dgrid_pp(self, level=3):
    """ Build a global grid and weights for a molecular integration (integration in 3-dimensional coordinate space) """
    from pyscf import dft
    from pyscf.nao.m_gauleg import gauss_legendre
    grid = dft.gen_grid.Grids(self)
    grid.level = level # precision as implemented in pyscf
    grid.radi_method=gauss_legendre
    atom2rcut=np.zeros(self.natoms)
    for ia,sp in enumerate(self.atom2sp): atom2rcut[ia] = self.ao_log.sp2rcut[sp]
    grid.build(atom2rcut=atom2rcut)
    return grid

  def build_3dgrid_ae(self, level=3):
    """ Build a global grid and weights for a molecular integration (integration in 3-dimensional coordinate space) """
    from pyscf import dft
    grid = dft.gen_grid.Grids(self)
    grid.level = level # precision as implemented in pyscf
    grid.build()
    return grid

  def comp_aos_den(self, coords):
    """ Compute the atomic orbitals for a given set of (Cartesian) coordinates. """
    from pyscf.nao.m_aos_libnao import aos_libnao
    if not self.init_sv_libnao_orbs : raise RuntimeError('not self.init_sv_libnao')
    return aos_libnao(coords, self.norbs)

  def comp_vnuc_coulomb(self, coords):
    ncoo = coords.shape[0]
    vnuc = np.zeros(ncoo)
    for R,sp in zip(self.atom2coord, self.atom2sp):
      dd, Z = cdist(R.reshape((1,3)), coords).reshape(ncoo), self.sp2charge[sp]
      vnuc = vnuc - Z / dd 
    return vnuc

  def vna(self, coords, sp2v=None):
    """ Compute the neutral-atom potential V_NA(coords) for a set of Cartesian coordinates coords.
        The subroutine could be also used for computing the non-linear core corrections or some other atom-centered fields."""
    sp2v = self.ao_log.sp2vna if sp2v is None else sp2v
    ncoo = coords.shape[0]
    vna = np.zeros(ncoo)
    for R,sp in zip(self.atom2coord, self.atom2sp):
      dd = cdist(R.reshape((1,3)), coords).reshape(ncoo)
      vnaa = self.ao_log.interp_rr(sp2v[sp], dd)
      vna = vna + vnaa 
    return vna

  def vna_coo(self, sp2v=None, **kw):
    """ Compute matrix elements of a potential which is given as superposition of central fields from each nuclei """
    from numpy import einsum, dot
    from scipy.sparse import coo_matrix
    sp2v = self.ao_log.sp2vna if sp2v is None else sp2v
    g = self.build_3dgrid_ae(**kw)
    ca2o = self.comp_aos_den(g.coords)
    vna = self.vna(g.coords, sp2v=sp2v)
    vna_w = g.weights*vna
    cb2vo = einsum('co,c->co', ca2o, vna_w)
    vna = dot(ca2o.T,cb2vo)
    return coo_matrix(vna)

  def init_libnao_orbs(self):
    """ Initialization of data on libnao site """
    from pyscf.nao.m_libnao import libnao
    from pyscf.nao.m_sv_chain_data import sv_chain_data
    from ctypes import POINTER, c_double, c_int64, c_int32, byref
    data = sv_chain_data(self)
    size_x = np.array([1,self.nspin,self.norbs,self.norbs,1], dtype=np.int32)
    libnao.init_sv_libnao_orbs.argtypes = (POINTER(c_double), POINTER(c_int64), POINTER(c_int32))
    libnao.init_sv_libnao_orbs(data.ctypes.data_as(POINTER(c_double)), c_int64(len(data)), size_x.ctypes.data_as(POINTER(c_int32)))
    self.init_sv_libnao_orbs = True

    libnao.init_aos_libnao.argtypes = (POINTER(c_int64), POINTER(c_int64))
    info = c_int64(-999)
    libnao.init_aos_libnao(c_int64(self.norbs), byref(info))
    if info.value!=0: raise RuntimeError("info!=0")
    return self

  def get_init_guess(self, key=None):
    """ Compute an initial guess for the density matrix. ???? """
    from pyscf.scf.hf import init_guess_by_minao
    if hasattr(self, 'mol'):
      dm = init_guess_by_minao(self.mol)
    else:
      dm = self.comp_dm()  # the loaded ks orbitals will be used
      if dm.shape[0:2]==(1,1) and dm.shape[4]==1 : dm = dm.reshape((self.norbs,self.norbs))
    return dm

  @property
  def nelectron(self):
    if self._nelectron is None:
      return tot_electrons(self)
    else:
      return self._nelectron

#
# Example of reading pySCF orbitals.
#
if __name__=="__main__":
  from pyscf import gto
  from pyscf.nao import nao
  import matplotlib.pyplot as plt
  """ Interpreting small Gaussian calculation """
  mol = gto.M(atom='O 0 0 0; H 0 0 1; H 0 1 0; Be 1 0 0', basis='ccpvtz') # coordinates in Angstrom!
  sv = nao(gto=mol, rcut_tol=1e-8, nr=512, rmin=1e-5)
  
  print(sv.ao_log.sp2norbs)
  print(sv.ao_log.sp2nmult)
  print(sv.ao_log.sp2rcut)
  print(sv.ao_log.sp_mu2rcut)
  print(sv.ao_log.nr)
  print(sv.ao_log.rr[0:4], sv.ao_log.rr[-1:-5:-1])
  print(sv.ao_log.psi_log[0].shape, sv.ao_log.psi_log_rl[0].shape)

  sp = 0
  for mu,[ff,j] in enumerate(zip(sv.ao_log.psi_log[sp], sv.ao_log.sp_mu2j[sp])):
    nc = abs(ff).max()
    if j==0 : plt.plot(sv.ao_log.rr, ff/nc, '--', label=str(mu)+' j='+str(j))
    if j>0 : plt.plot(sv.ao_log.rr, ff/nc, label=str(mu)+' j='+str(j))

  plt.legend()
  #plt.xlim(0.0, 10.0)
  #plt.show()
