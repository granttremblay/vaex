# -*- coding: utf-8 -*-
import mmap
import os
import math
import time
import itertools
import gavifast
import functools
import collections
import sys
import platform

import h5py
import numpy as np

from vaex.utils import Timer
from vaex import multithreading
import vaex.events
import vaex.ui.undo

frozen = getattr(sys, 'frozen', False)
darwin = "darwin" not in platform.system()
import astropy.io.fits as fits
#if (not frozen) or darwin: # astropy not working with pyinstaller
#	#fits = __import__("astropy.io.fits").io.fits
#	pass

def error(title, msg):
	print "Error", title, msg

sys_is_le = sys.byteorder == 'little'
native_code = sys_is_le and '<' or '>'

import vaex.logging
logger = vaex.logging.getLogger("vaex")


dataset_type_map = {}


class Link(object):
	def __init__(self, dataset):
		self.dataset = dataset
		self.listeners = []
		
	def sendExpression(self, expression, receiver):
		for listener in self.listeners:
			if listener != receiver:
				listener.onChangeExpression(expression)
		if expression in self.dataset.global_links:
			# merge the listeners of this link to the other link
			logger.debug("merging with link %r" % expression)
			merging_link = self.dataset.global_links[expression]
			for receiver in merging_link.listeners:
				self.listeners.append(receiver)
		else:
			# add new mapping
			logger.debug("renamed link %r" % expression)
			self.dataset.global_links[expression] = self
		# remove old mapping
		for key, link in self.dataset.global_links.items():
			logger.debug("link[%r] = %r" % (key, link))
			if (link == self) and key != expression: # remove dangling links
				logger.debug("removing link %r" % key)
				del self.dataset.global_links[key]
				
	def sendRanges(self, range_, receiver):
		for listener in self.listeners:
			if listener != receiver:
				listener.onChangeRange(range_)

	def sendRangesShow(self, range_, receiver):
		for listener in self.listeners:
			if listener != receiver:
				listener.onChangeRangeShow(range_)

	#
	@staticmethod
	def sendCompute(links, receivers):
		listener_set = set(list(itertools.chain.from_iterable([link.listeners for link in links])))

		for listener in listener_set:
			if listener not in receivers:
				listener.onCompute()

	def sendPlot(self, receiver):
		for listener in self.listeners:
			if listener != receiver:
				listener.onPlot()
				
				

import execution
import vaex.grids
import multithreading

class Dataset(object):
	def __init__(self, name):
		self.name = name

	def plot2d(self, x, y, vx=None, vy=None, size=256, size_vector=32, xlim=None, ylim=None):
		import vaex.plot
		if not xlim and not ylim:
			xlim, ylim = execution.main_manager.find_min_max(self, [x, y])
		elif not xlim:
			xlim = execution.main_manager.find_min_max(self, [x])
		elif not ylim:
			ylim = execution.main_manager.find_min_max(self, [y])

		grids = vaex.grids.Grids(self, multithreading.pool, x, y)
		grids.ranges = [xlim, ylim]
		grids.define_grid("counts", size, None)
		if vx and vy:
			grids.define_grid("vx", size_vector, vx)
			grids.define_grid("vy", size_vector, vy)
		grids.add_jobs(execution.main_manager)
		execution.main_manager.execute()
		vaex.plot.grid(grids["counts"])
		#vaex.plot.vector2d(grids["vx"], grids["vy"])




class DatasetLocal(Dataset):
	def __init__(self, name, path):
		self.is_local = True
		super(DatasetLocal, self).__init__(name)
		self.path = path

class DatasetRemote(Dataset):
	def __init__(self, name, server):
		self.is_local = False
		super(DatasetRemote, self).__init__(name)
		self.server = server


class DatasetMemoryMapped(DatasetLocal):
	def link(self, expression, listener):
		if expression not in self.global_links:
			self.global_links[expression] = Link(self)
			logger.debug("creating link object: %r" % self.global_links[expression])
		else:
			
			logger.debug("reusing link object: %r" % self.global_links[expression])

			
		link = self.global_links[expression]
		link.listeners.append(listener)
		return link
	
	def unlink(self, link, receiver):
		link.listeners.remove(receiver)
		
	def full_length(self):
		return self._length
		
	def __len__(self):
		return self._fraction_length
		
	def length(self, selection=False):
		if selection:
			return 0 if self.mask is None else np.sum(self.mask)
		else:
			return len(self)
		
	def byte_size(self, selection=False):
		bytes_per_row = 0
		for column in self.columns.values():
			dtype = column.dtype
			bytes_per_row += dtype.itemsize
		return bytes_per_row * self.length(selection=selection)
		
		
	# nommap is a hack to get in memory datasets working
	def __init__(self, filename, write=False, nommap=False, name=None):
		super(DatasetMemoryMapped, self).__init__(name=name or os.path.splitext(os.path.basename(filename))[0], path=os.path.abspath(filename) if filename is not None else None)
		self.filename = filename or "no file"
		self.write = write
		#self.name = name or os.path.splitext(os.path.basename(self.filename))[0]
		#self.path = os.path.abspath(filename) if filename is not None else None
		self.nommap = nommap
		if not nommap:
			self.file = file(self.filename, "r+" if write else "r")
			self.fileno = self.file.fileno()
			self.mapping = mmap.mmap(self.fileno, 0, prot=mmap.PROT_READ | 0 if not write else mmap.PROT_WRITE )
			self.file_map = {filename: self.file}
			self.fileno_map = {filename: self.fileno}
			self.mapping_map = {filename: self.mapping}
		else:
			self.file_map = {}
			self.fileno_map = {}
			self.mapping_map = {}
		self._length = None
		self._fraction_length = None
		self.nColumns = 0
		self.columns = {}
		self.column_names = []
		self.rank1s = {}
		self.rank1names = []
		self.virtual_columns = collections.OrderedDict()
		
		self.axes = {}
		self.axis_names = []
		
		# these are replaced by variables
		#self.properties = {}
		#self.property_names = []

		self.current_slice = None
		self.fraction = 1.0
		
		
		self.selected_row_index = None
		self.selected_serie_index = 0
		self.row_selection_listeners = []
		self.serie_index_selection_listeners = []
		self.mask_listeners = []
		self.all_columns = {}
		self.all_column_names = []
		self.mask = None
		self.global_links = {}
		
		self.offsets = {}
		self.strides = {}
		self.filenames = {}
		self.dtypes = {}
		self.samp_id = None
		self.variables = collections.OrderedDict()
		
		self.signal_pick = vaex.events.Signal("pick")
		self.signal_sequence_index_change = vaex.events.Signal("sequence index change")

		self.undo_manager = vaex.ui.undo.UndoManager()

	def has_snapshots(self):
		return len(self.rank1s) > 0

	def get_path(self):
		return self.path
		
	def get_column_names(self):
		names = list(self.column_names)
		for vname in self.virtual_columns.keys():
			if vname not in names:
				names.append(vname)
		return names
		
		
	def evaluate(self, callback, *expressions, **variables):
		jobManager = JobsManager()
		logger.debug("evalulate: %r %r" % (expressions,variables))
		jobManager.addJob(0, callback, self, *expressions, **variables)
		jobManager.execute()
		return
		
		
	def old(self):
		class Info(object):
			pass
		outputs = [np.zeros(buffer_size, dtype=np.float64) for _ in expressions]
		n_blocks = int(math.ceil(self._length *1.0 / buffer_size))
		print "blocks", n_blocks, self._length *1.0 / buffer_size
		# execute blocks for all expressions, better for Lx cache
		for block_index in range(n_blocks):
			i1 = block_index * buffer_size
			i2 = (block_index +1) * buffer_size
			if i2 >= self._length: # round off the sizes
				i2 = self._length
				for i in range(len(outputs)):
					outputs[i] = outputs[i][:i2-i1]
			# local dicts has slices (not copies) of the whole dataset
			local_dict = {}
			for key, value in self.columns.items():
				local_dict[key] = value[i1:i2]
			info = Info()
			info.index = block_index
			info.size = i2-i1
			info.length = n_blocks
			info.first = block_index == 0
			info.i1 = i1
			info.i2 = i2
			info.slice = slice(i1, i2)
			results = []
			for output, expression in zip(outputs, expressions):
				if expression in self.column_names and self.columns[expression].dtype == np.float64:
					print "avoided"
					#yield self.columns[expression][i1:i2], info
					results.append(self.columns[expression][i1:i2])
				else:
					ne.evaluate(expression, local_dict=local_dict, out=output, casting="unsafe")
					results.append(output)
			print results, info
			yield tuple(results), info
		
		
	def addFile(self, filename, write=False):
		self.file_map[filename] = file(filename, "r+" if write else "r")
		self.fileno_map[filename] = self.file_map[filename].fileno()
		self.mapping_map[filename] = mmap.mmap(self.fileno_map[filename], 0, prot=mmap.PROT_READ | 0 if not write else mmap.PROT_WRITE )


	def selectMask(self, mask):
		self.mask = mask
		for mask_listener in self.mask_listeners:
			mask_listener(mask)
		
		
	def selectRow(self, index):
		self.selected_row_index = index
		logger.debug("emit pick signal: %r" % index)
		self.signal_pick.emit(index)
		for row_selection_listener in self.row_selection_listeners:
			row_selection_listener(index)
		
	def selectSerieIndex(self, serie_index):
		self.selected_serie_index = serie_index
		for serie_index_selection_listener in self.serie_index_selection_listeners:
			serie_index_selection_listener(serie_index)
		self.signal_sequence_index_change.emit(self, serie_index)
			
	def matches_url(self, url):
		filename = url
		if filename.startswith("file:/"):
			filename = filename[5:]
		similar = os.path.splitext(os.path.abspath(self.filename))[0] == os.path.splitext(filename)[0]
		logger.info("matching urls: %r == %r == %r" % (os.path.splitext(self.filename)[0], os.path.splitext(filename)[0], similar) )
		return similar
			
		
		
	def close(self):
		self.file.close()
		self.mapping.close()
		
	def setFraction(self, fraction):
		self.fraction = fraction
		self.current_slice = (0, int(self._length * fraction))
		self._fraction_length = self.current_slice[1]
		self.selectMask(None)
		# TODO: if row in slice, we don't have to remove it
		self.selectRow(None)
		
	def __addMemoryColumn(self, name, column):
		# remove, is replaced by array argument of addColumn
		length = len(column)
		if self.current_slice is None:
			self.current_slice = (0, length)
			self.fraction = 1.
			self._fraction_length = length
		self._length = length
		#print self.mapping, dtype, length if stride is None else length * stride, offset
		self.columns[name] = column
		self.column_names.append(name)
		self.all_columns[name] = column
		self.all_column_names.append(name)
		#self.column_names.sort()
		self.nColumns += 1
		self.nRows = self._length
		

	def addAxis(self, name, offset=None, length=None, dtype=np.float64, stride=1, filename=None):
		if filename is None:
			filename = self.filename
		mapping = self.mapping_map[filename]
		mmapped_array = np.frombuffer(mapping, dtype=dtype, count=length if stride is None else length * stride, offset=offset)
		if stride:
			mmapped_array = mmapped_array[::stride]
		self.axes[name] = mmapped_array
		self.axis_names.append(name)
	
		
	def addColumn(self, name, offset=None, length=None, dtype=np.float64, stride=1, filename=None, array=None):
		if filename is None:
			filename = self.filename
		if not self.nommap:
			mapping = self.mapping_map[filename]
			
		if array is not None:
			length = len(array)
			
		if self._length is not None and length != self._length:
			error("inconsistent length", "length of column %s is %d, while %d was expected" % (name, length, self._length))
		else:
			if self.current_slice is None:
				self.current_slice = (0, length)
				self.fraction = 1.
				self._fraction_length = length
			self._length = length
			#print self.mapping, dtype, length if stride is None else length * stride, offset
			if array is not None:
				length = len(array)
				mmapped_array = array
				stride = None
				offset = None
				dtype = array.dtype
			else:
				if offset is None:
					print "offset is None"
					sys.exit(0)
				mmapped_array = np.frombuffer(mapping, dtype=dtype, count=length if stride is None else length * stride, offset=offset)
				if stride:
					#import pdb
					#pdb.set_trace()
					mmapped_array = mmapped_array[::stride]
			self.columns[name] = mmapped_array
			self.column_names.append(name)
			self.all_columns[name] = mmapped_array
			self.all_column_names.append(name)
			#self.column_names.sort()
			self.nColumns += 1
			self.nRows = self._length
			self.offsets[name] = offset
			self.strides[name] = stride
			if filename is not None:
				self.filenames[name] = os.path.abspath(filename)
			self.dtypes[name] = dtype
			
	def addRank1(self, name, offset, length, length1, dtype=np.float64, stride=1, stride1=1, filename=None, transposed=False):
		if filename is None:
			filename = self.filename
		mapping = self.mapping_map[filename]
		if (not transposed and self._length is not None and length != self._length) or (transposed and self._length is not None and length1 != self._length):
			error("inconsistent length", "length of column %s is %d, while %d was expected" % (name, length, self._length))
		else:
			if self.current_slice is None:
				self.current_slice = (0, length if not transposed else length1)
				self.fraction = 1.
				self._fraction_length = length if not transposed else length1
			self._length = length if not transposed else length1
			#print self.mapping, dtype, length if stride is None else length * stride, offset
			rawlength = length * length1
			rawlength *= stride
			rawlength *= stride1
			#print rawlength, offset
			#print rawlength * 8, offset, self.mapping.size()
			#import pdb
			#pdb.set_trace()
			mmapped_array = np.frombuffer(mapping, dtype=dtype, count=rawlength, offset=offset)
			mmapped_array = mmapped_array.reshape((length1*stride1, length*stride))
			mmapped_array = mmapped_array[::stride1,::stride]
			#if transposed:
			#	mmapped_array = mmapped_array.T
			#assert mmapped_array.shape[1] == self._length, "error {0} {1} {2} {3} {4}".format(length, length1, mmapped_array.shape, self._length, transposed)
			self.rank1s[name] = mmapped_array
			self.rank1names.append(name)
			self.all_columns[name] = mmapped_array
			self.all_column_names.append(name)
			
			#self.column_names.sort()
			#self.nColumns += 1
			#self.nRows = self._length
			#self.columns[name] = mmapped_array
			#self.column_names.append(name)
			
	@classmethod
	def can_open(cls, path, *args):
		return False
	
	@classmethod
	def get_options(cls, path):
		return []
	
	@classmethod
	def option_to_args(cls, option):
		return []

import struct
class HansMemoryMapped(DatasetMemoryMapped):
	def __init__(self, filename, filename_extra=None):
		super(HansMemoryMapped, self).__init__(filename)
		self.pageSize, \
		self.formatSize, \
		self.numberParticles, \
		self.numberTimes, \
		self.numberParameters, \
		self.numberCompute, \
		self.dataOffset, \
		self.dataHeaderSize = struct.unpack("Q"*8, self.mapping[:8*8])
		zerooffset = offset = self.dataOffset
		length = self.numberParticles+1
		stride = self.formatSize/8 # stride in units of the size of the element (float64)
		
		# TODO: ask Hans for the self.numberTimes-2
		lastoffset = offset + (self.numberParticles+1)*(self.numberTimes-2)*self.formatSize
		t_index = 3
		names = "x y z vx vy vz".split()
		midoffset = offset + (self.numberParticles+1)*self.formatSize*t_index
		names = "x y z vx vy vz".split()

		for i, name in enumerate(names):
			self.addColumn(name+"_0", offset+8*i, length, dtype=np.float64, stride=stride)
			
		for i, name in enumerate(names):
			self.addColumn(name+"_last", lastoffset+8*i, length, dtype=np.float64, stride=stride)
		
		#for i, name in enumerate(names):
		#	self.addColumn(name+"_mid", midoffset+8*i, length, dtype=np.float64, stride=stride)
		

		names = "x y z vx vy vz".split()
		#import pdb
		#pdb.set_trace()
		if 1:
			stride = self.formatSize/8 
			#stride1 = self.numberTimes #*self.formatSize/8 
			for i, name in enumerate(names):
				# TODO: ask Hans for the self.numberTimes-1
				self.addRank1(name, offset+8*i, length=self.numberParticles+1, length1=self.numberTimes-1, dtype=np.float64, stride=stride, stride1=1)

		if filename_extra is None:
			basename = os.path.basename(filename)
			if os.path.exists(basename + ".omega2"):
				filename_extra = basename + ".omega2"

		if filename_extra is not None:
			self.addFile(filename_extra)
			mapping = self.mapping_map[filename_extra]
			names = "J_r J_theta J_phi Theta_r Theta_theta Theta_phi Omega_r Omega_theta Omega_phi r_apo r_peri".split()
			offset = 0
			stride = 11
			#import pdb
			#pdb.set_trace()
			for i, name in enumerate(names):
				# TODO: ask Hans for the self.numberTimes-1
				self.addRank1(name, offset+8*i, length=self.numberParticles+1, length1=self.numberTimes-1, dtype=np.float64, stride=stride, stride1=1, filename=filename_extra)

				self.addColumn(name+"_0", offset+8*i, length, dtype=np.float64, stride=stride, filename=filename_extra)
				self.addColumn(name+"_last", offset+8*i + (self.numberParticles+1)*(self.numberTimes-2)*11*8, length, dtype=np.float64, stride=stride, filename=filename_extra)
			
			

		#for i, name in enumerate(names):
		#	self.addColumn(name+"_last", offset+8*i + (self.formatSize*(self.numberTimes-1)), length, dtype=np.float64, stride=stride)
		#for i, name in enumerate(names):
		#	self.addRank1(name, offset+8*i, (length, numberTimes), dtype=np.float64, stride=stride)
		
		
		
		
		#uint64 = np.frombuffer(self.mapping, dtype=dtype, count=length if stride is None else length * stride, offset=offset)

	@classmethod
	def can_open(cls, path, *args):
		basename, ext = os.path.splitext(path)
		if os.path.exists(basename + ".omega2"):
			return True
		return False

	@classmethod
	def get_options(cls, path):
		return []

	@classmethod
	def option_to_args(cls, option):
		return []


dataset_type_map["buist"] = HansMemoryMapped

if __name__ == "__main__":
	path = "/Users/users/buist/research/2014 Simulation Data/12Orbits/Sigma/Orbitorb1.ac0.10000.100.5.orb.omega2"
	path = "/net/pannekoek/data/users/buist/Research/2014 Simulation Data/12Orbits/Integration/Orbitorb9.ac8.10000.100.5.orb.bin"

	hmm = HansMemoryMapped(path)


class FitsBinTable(DatasetMemoryMapped):
	def __init__(self, filename, write=False):
		super(FitsBinTable, self).__init__(filename, write=write)
		fitsfile = fits.open(filename)
		for table in fitsfile:
			if isinstance(table, fits.BinTableHDU):
				table_offset = table._data_offset
				#import pdb
				#pdb.set_trace()
				if table.columns[0].dim is not None: # for sure not a colfits
					dim = eval(table.columns[0].dim) # TODO: can we not do an eval here? not so safe
					if dim[0] == 1 and len(dim) == 2: # we have colfits format
						logger.debug("colfits file!")
						offset = table_offset
						for i in range(len(table.columns)):
							column = table.columns[i]
							cannot_handle = False
							#print column.name, str(column.dtype)
							#try:
							#	dtype, length = eval(str(column.dtype)) # ugly hack
							#	length = length[0]
							#except:
							#	cannot_handle = True

							# flatlength == length * arraylength
							flatlength, fitstype = long(column.format[:-1]),column.format[-1]
							arraylength, length = arrayshape = eval(column.dim)

							# numpy dtype code, like f8, i4
							dtypecode = astropy.io.fits.column.FITS2NUMPY[fitstype]


							dtype = np.dtype((">" +dtypecode, arraylength))
							if 0:
								if arraylength > 1:
									dtype = np.dtype((">" +dtypecode, arraylength))
								else:
									if dtypecode == "a": # I think numpy needs by default a length 1
										dtype = np.dtype(dtypecode + "1")
									else:
										dtype = np.dtype(">" +dtypecode)
								#	bytessize = 8

							bytessize = dtype.itemsize
							print column.name, dtype, column.format, column.dim, length, bytessize, arraylength
							#if not cannot_handle:
							if (flatlength > 0) and dtypecode != "a": # TODO: support strings
								#print column.name, dtype, length
								#print "ok", column.dtype
								#if type == np.float64:
								#print "\t", offset, dtype, length
								#typestr = eval(str(table.columns[i].dtype))[0].replace("<", ">").strip()
								#print "   type", typestr
								#dtype = np.zeros(1,dtype=typestr).dtype
								#if "f" in dtype:
								if 1:
									#dtype = np.dtype(dtype)
									#print "we have float64!", dtype
									#dtype = ">f8"
									self.addColumn(column.name, offset=offset, dtype=dtype, length=length)
									#col = self.columns[column.name]
									#print "   ", col[:10],  col[:10].dtype, col.dtype.byteorder == native_code, bytessize

							if flatlength > 0: # flatlength can be
								offset += bytessize * length

							#else:
							if 0:
								#print str(column.dtype)
								#print column.dtype
								#pdb.set_trace()
								print column.name, column.format, length
								#print column.dtype.descr
								#print column.dtype.fields
								#assert column.dtype.descr[0][1][0] == "|"
								#assert column.dtype.descr[0][1][1] == "S"
								#overflown_length =
								#import pdb
								#pdb.set_trace()
								offset += eval(column.dim)[0] * length
								#raise Exception,"cannot handle type: %s" % column.dtype
								#sys.exit(0)
							print "offset=", offset, 403532029440-offset
					#sys.exit(0)

				else:
					logger.debug("adding table: %r" % table)
					for column in table.columns:
						array = column.array[:]
						array = column.array[:] # 2nd time it will be a real np array
						#import pdb
						#pdb.set_trace()
						if array.dtype.kind in "fi":
							self.addColumn(column.name, array=array)
		#BinTableHDU
	@classmethod
	def can_open(cls, path, *args):
		return os.path.splitext(path)[1] == ".fits"
	
	@classmethod
	def get_options(cls, path):
		return [] # future: support multiple tables?
	
	@classmethod
	def option_to_args(cls, option):
		return []

dataset_type_map["fits"] = FitsBinTable
		
		
class InMemoryTable(DatasetMemoryMapped):
	def __init__(self, filename, write=False):
		super(InMemoryTable, self).__init__(filename)
		
		if 1:
			
			N = 1024
			x = np.arange(1, N+1, dtype=np.float64)
			x, y = np.meshgrid(x, x)
			shape = x.shape
			phase = np.random.random(shape)* 2 * np.pi
			#r = (N-x)**2 + (N-y)**2
			r = (x)**2 + (y)**2
			amplitude = np.random.random(shape) * np.exp(-r**1.4/100**2) * np.exp( 1j * phase)
			amplitude[0,0] = 0
			import pylab
			realspace = np.fft.fft2(amplitude)
			vx = np.fft.fft2(amplitude * x).real
			vy = np.fft.fft2(amplitude * y).real
			x = np.arange(1, N+1, dtype=np.float64)
			x, y = np.meshgrid(x, x)
			scale = 0.05
			for i in range(2):
				x += vx * scale
				y += vy * scale
			
			self.addColumn("x", array=x.reshape(-1))
			self.addColumn("y", array=y.reshape(-1))
			return
				
			if 0:
				pass
			pylab.imshow(realspace.real)
			pylab.show()
			sys.exit(0)
		
			N = 512
			d = 2
			
			x = np.arange(N)
			x, y = np.meshgrid(x, x)
			x = x.reshape(-1)
			y = y.reshape(-1)
			x = np.random.random(x.shape) * 0.5 + 0.5
			y = np.random.random(x.shape) * 0.5 + 0.5
			shape = x.shape
			grid = np.zeros(shape, dtype=np.float64)
			gavifast.histogram2d(x, y, None, grid, 0, N, 0, N)
			phi_f = np.fft.fft2(grid)
			self.addColumn("x", array=x)
			self.addColumn("y", array=y)
			
			
			
			#print x.shape, x.dtype
			
			#sys.exit(0)
			
			
			return
		
		
		eta = 4
		max_level = 13
		dim = 2
		N = eta**(max_level)
		array = np.zeros((dim, N), dtype=np.float64)
		L = 1.6
		#print "size {:,}".format(N)
		
		
		def do(center, size, index, level):
			pos = center.reshape((-1,1)) + np.random.random((dim, eta)) * size - size/2
			#array[:,index:index+eta] = pos
			if level == max_level:
				array[:,index:index+eta] = pos
				return index+eta
			else:
				for i in range(eta):
					index = do(pos[:,i], size/L, index, level+1)
				return index
			
		#do(np.zeros(dim), 1., 0, 0)
		for d in range(dim):
			gavifast.soneira_peebles(array[d], 0, 1, L, eta, max_level)
		for i, name in zip(range(dim), "x y z w v u".split()):
			self.addColumn(name, array=array[i])
		
		return
		
		
		N = int(1e7)
		a0 = 1.
		t = np.linspace(0, 2 * np.pi * 5, N) + 2 * np.pi/1000 * (np.random.random() - 0.5)
		a0 = a0 - t /t.max() * a0 * 0.5
		a = np.zeros(N) + a0 + a0 * 0.1 * (np.random.random(N) - 0.5)
		b = 0.2
		x = a * (np.cos(t) + 0.2 * (np.random.random(N) - 0.5))
		y = a * (np.sin(t) + 0.2 * (np.random.random(N) - 0.5))
		z = b * t
		
		self.addColumn("x", array=x)
		self.addColumn("y", array=y)
		self.addColumn("z", array=z)
		self.addColumn("a", array=a)
		self.addColumn("t", array=t)
		return

		#for i in range(N):
		#	a[
		
		
		N = 2+4+8+16+32+64+128
		rand = np.random.random(N-1)
		rand_y = np.random.random(N-1)
		#x = 
		xlist =[]
		ylist =[]
		for i in range(15000*2):
			#random.seed(0)
			index = 0
			level = 0
			offset = 0
			x1 = 0.
			x2 = 1.
			xs = []
			ys = []
			for j in range(7):
				#level = 5 - j
				Nlevel = 2**(level+1)
				#offset = sum(
				u1 = np.random.random()
				u2 = np.random.random()
				#c = rand[offset:offset+Nlevel].min()
				#c = 0
				#v1 = rand[offset+index] - c
				#v2 = rand[offset+index+1] - c
				#assert v1 >= 0
				#assert v2 >= 0
				cumulative = np.cumsum(rand[offset:offset+Nlevel])
				cumulative = np.cumsum(np.arange(Nlevel))
				cumulative = []
				total = 0
				for value in rand[offset:offset+Nlevel]:
					total += value
					cumulative.append(total)
				cumulative = np.array(cumulative)
				cumulative = cumulative * 1./cumulative[-1]
				for i, value in enumerate(cumulative):
					if value >= u1:
						break
				left, mid, right = [(float(i+1+j/2.*2))/(Nlevel+1) for j in [-1,0,1]]
				x  = np.random.triangular(left, mid, right)

				cumulative = []
				total = 0
				for value in rand_y[offset:offset+Nlevel]:
					total += value
					cumulative.append(total)
				cumulative = np.array(cumulative)
				cumulative = cumulative * 1./cumulative[-1]
				for i, value in enumerate(cumulative):
					if value >= u2:
						break
				left, mid, right = [(float(i+1+j/2.*2))/(Nlevel+1) for j in [-1,0,1]]
				y  = np.random.triangular(left, mid, right)
				if 0:
					if v1 < v2:
						b = v1
						c = v2-v1
						w = (-b + np.sqrt(b**2.+4.*c*u )) /   (-b + np.sqrt(b**2.+4.*c ))
						x = x1 + w * (x2-x1)
					else:
						b = v2
						c = v1-v2
						w = 1. - (-b + np.sqrt(b**2.+4.*c*u )) /   (-b + np.sqrt(b**2.+4.*c ))
						x = x2 - (x2-x1)*w
				#w = np.sqrt(r)
				#xs.append(x1 + w * (x2-x1))
				xs.append(x)# - (x1+x2)/2.)
				ys.append(y)
				if 0:
					if w < 0.5:
						x1, x2 = x1, x1 + (x2-x1)/2.
						index = index * 2
						#offset += Nlevel
					else:
						x1, x2 =  x1 + (x2-x1)/2., x2
						#offset += Nlevel*2
						index = (index+1) * 2
				level += 1
				offset += Nlevel
				#if np.random.random() < 0.21:
				#	break
			#print
			#xs = [np.sqrt(np.random.random())]
			amplitudes = 1./(np.arange(len(xs)) + 1)**2
			#xlist.append( np.sum( (xs*amplitudes)/np.sum(amplitudes) )  )
			#xlist.append( (xs[0] + xs[1] * 0.5)/1.5  )
			#xlist.append(sum(xs * amplitudes))
			#xlist.append(sum(xs))
			#xlist.append(xs[4])
			xlist.extend(xs[3:])
			ylist.extend(ys[3:])
			

		self.addColumn("x", array=np.array(xlist))
		self.addColumn("y", array=np.array(ylist))
		#self.addColumn("x", array=np.random.random(10000)**0.5)
		
		return
				
				#if random.
				
		
		x = []
		y = []
		z = []
		
		for i in range(100):
			x0, y0, z0 = 0., 0., 0.
			vx, vy, vz = 1., 0., 0.
			for i in range(1000):
				x0 += vx
				y0 += vy
				z0 += vz
				x.append(x0)
				y.append(y0)
				z.append(z0)
				s = 0.01
				vx += np.random.random() * s-s/2
				vy += np.random.random() * s-s/2
				vz += np.random.random() * s-s/2
				if np.random.random() < 0.05:
					s = 1.
					vx += np.random.random() * s-s/2
					#vz += np.random.random() * s-s/2
					
		x = np.array(x)
		y = np.array(y)
		z = np.array(z)
		self.addColumn("x", array=x)
		self.addColumn("y", array=y)
		self.addColumn("z", array=z)
			
		
dataset_type_map["fits"] = FitsBinTable
		
class Hdf5MemoryMapped(DatasetMemoryMapped):
	def __init__(self, filename, write=False):
		super(Hdf5MemoryMapped, self).__init__(filename, write=write)
		self.h5file = h5py.File(self.filename, "r+" if write else "r")
		try:
			self.load()
		finally:
			self.h5file.close()
		
	@classmethod
	def can_open(cls, path, *args):
		h5file = None
		try:
			h5file = h5py.File(path, "r")
		except:
			logger.exception("could not open file as hdf5")
			return False
		if h5file is not None:
			return ("data" in h5file) or ("columns" in h5file)
		else:
			logger.debug("file %s has no data or columns group" % path)
		return False
			
	
	@classmethod
	def get_options(cls, path):
		return []
	
	@classmethod
	def option_to_args(cls, option):
		return []

	def load(self):
		if "data" in self.h5file:
			self.load_columns(self.h5file["/data"])
		if "columns" in self.h5file:
			self.load_columns(self.h5file["/columns"])
		if "properties" in self.h5file:
			self.load_variables(self.h5file["/properties"]) # old name, kept for portability
		if "variables" in self.h5file:
			self.load_variables(self.h5file["/variables"])
		if "axes" in self.h5file:
			self.load_axes(self.h5file["/axes"])
			
	#def 
	def load_axes(self, axes_data):
		for name in axes_data:
			axis = axes_data[name]
			logger.debug("loading axis %r" % name)
			offset = axis.id.get_offset() 
			shape = axis.shape
			assert len(shape) == 1 # ony 1d axes
			#print name, offset, len(axis), axis.dtype
			self.addAxis(name, offset=offset, length=len(axis), dtype=axis.dtype)
			#self.axis_names.append(axes_data)
			#self.axes[name] = np.array(axes_data[name])
			
	def load_variables(self, h5variables):
		for key, value in h5variables.attrs.iteritems():
			self.variables[key] = value
			
			
	def load_columns(self, h5data):
		#print h5data
		# make sure x y x etc are first
		first = "x y z vx vy vz".split()
		finished = set()
		for column_name in first + list(h5data):
			if column_name in h5data and column_name not in finished:
				#print type(column_name)
				column = h5data[column_name]
				if hasattr(column, "dtype"):
					#print column, column.shape
					offset = column.id.get_offset() 
					if offset is None:
						raise Exception, "columns doesn't really exist in hdf5 file"
					shape = column.shape
					if len(shape) == 1:
						self.addColumn(column_name, offset, len(column), dtype=column.dtype)
					else:

						#transposed = self._length is None or shape[0] == self._length
						transposed = shape[1] < shape[0]
						self.addRank1(column_name, offset, shape[1], length1=shape[0], dtype=column.dtype, stride=1, stride1=1, transposed=transposed)
						#if len(shape[0]) == self._length:
						#	self.addRank1(column_name, offset, shape[1], length1=shape[0], dtype=column.dtype, stride=1, stride1=1)
						#self.addColumn(column_name+"_0", offset, shape[1], dtype=column.dtype)
						#self.addColumn(column_name+"_last", offset+(shape[0]-1)*shape[1]*column.dtype.itemsize, shape[1], dtype=column.dtype)
						#self.addRank1(name, offset+8*i, length=self.numberParticles+1, length1=self.numberTimes-1, dtype=np.float64, stride=stride, stride1=1, filename=filename_extra)
			finished.add(column_name)
			
	def close(self):
		super(Hdf5MemoryMapped, self).close()
		self.h5file.close()
		
	def __expose_array(self, hdf5path, column_name):
		array = self.h5file[hdf5path]
		array[0] = array[0] # without this, get_offset returns None, probably the array isn't really created
		offset = array.id.get_offset() 
		self.remap()
		self.addColumn(column_name, offset, len(array), dtype=array.dtype)
		
	def __add_column(self, column_name, dtype=np.float64, length=None):
		array = self.h5data.create_dataset(column_name, shape=(self._length if length is None else length,), dtype=dtype)
		array[0] = array[0] # see above
		offset = array.id.get_offset() 
		self.h5file.flush()
		self.remap()
		self.addColumn(column_name, offset, len(array), dtype=array.dtype)

dataset_type_map["h5gavi"] = Hdf5MemoryMapped

class AmuseHdf5MemoryMapped(Hdf5MemoryMapped):
	def __init__(self, filename, write=False):
		super(AmuseHdf5MemoryMapped, self).__init__(filename, write=write)
		
	@classmethod
	def can_open(cls, path, *args):
		h5file = None
		try:
			h5file = h5py.File(path, "r")
		except:
			return False
		if h5file is not None:
			return ("particles" in h5file)# or ("columns" in h5file)
		return False

	def load(self):
		particles = self.h5file["/particles"]
		for group_name in particles:
			#import pdb
			#pdb.set_trace()
			group = particles[group_name]
			self.load_columns(group["attributes"])
			
			column_name = "keys"
			column = group[column_name]
			offset = column.id.get_offset() 
			self.addColumn(column_name, offset, len(column), dtype=column.dtype)

dataset_type_map["amuse"] = AmuseHdf5MemoryMapped

class Hdf5MemoryMappedGadget(DatasetMemoryMapped):
	def __init__(self, filename, particleName=None, particleType=None):
		if "#" in filename:
			filename, index = filename.split("#")
			index = int(index)
			particleNames = "gas halo disk bulge stars dm".split()
			particleType = index 
			particleName = particleNames[particleType]
			
		super(Hdf5MemoryMappedGadget, self).__init__(filename)
		self.particleType = particleType
		self.particleName = particleName
		self.name = self.name + "-" + self.particleName
		h5file = h5py.File(self.filename, 'r')
		#for i in range(1,4):
		key = "/PartType%d" % self.particleType
		if key not in h5file:
			raise KeyError, "%s does not exist" % key
		particles = h5file[key]
		for name in particles.keys():
			#name = "/PartType%d/Coordinates" % i
			data = particles[name]
			if isinstance(data, h5py.highlevel.Dataset): #array.shape
				array = data
				shape = array.shape
				if len(shape) == 1:
					offset = array.id.get_offset()
					if offset is not None:
						self.addColumn(name, offset, data.shape[0], dtype=data.dtype)
				else:
					if name == "Coordinates":
						offset = data.id.get_offset() 
						if offset is None:
							print name, "is not of continuous layout?"
							sys.exit(0)
						bytesize = data.dtype.itemsize
						self.addColumn("x", offset, data.shape[0], dtype=data.dtype, stride=3)
						self.addColumn("y", offset+bytesize, data.shape[0], dtype=data.dtype, stride=3)
						self.addColumn("z", offset+bytesize*2, data.shape[0], dtype=data.dtype, stride=3)
					elif name == "Velocity":
						offset = data.id.get_offset() 
						self.addColumn("vx", offset, data.shape[0], dtype=data.dtype, stride=3)
						self.addColumn("vy", offset+bytesize, data.shape[0], dtype=data.dtype, stride=3)
						self.addColumn("vz", offset+bytesize*2, data.shape[0], dtype=data.dtype, stride=3)
					elif name == "Velocities":
						offset = data.id.get_offset() 
						self.addColumn("vx", offset, data.shape[0], dtype=data.dtype, stride=3)
						self.addColumn("vy", offset+bytesize, data.shape[0], dtype=data.dtype, stride=3)
						self.addColumn("vz", offset+bytesize*2, data.shape[0], dtype=data.dtype, stride=3)
					else:
						logger.error("unsupported column: %r of shape %r" % (name, array.shape))
		if "Header" in h5file:
			for name in "Redshift Time_GYR".split():
				if name in h5file["Header"].attrs:
					value = h5file["Header"].attrs[name]
					logger.debug("property[{name!r}] = {value}".format(**locals()))
					self.variables[name] = value
					#self.property_names.append(name)
		
		name = "particle_type"
		value = particleType
		logger.debug("property[{name}] = {value}".format(**locals()))
		self.variables[name] = value
		#self.property_names.append(name)

	@classmethod
	def can_open(cls, path, *args):
		if len(args) == 2:
			particleName = args[0]
			particleType = args[1]
		else:
			logger.debug("try particle type")
			try:
				filename, index = path.split("#")
				index = int(index)
				particleNames = "gas halo disk bulge stars dm".split()
				particleType = index 
				particleName = particleNames[particleType]
				path = filename
			except:
				logger.info("cannot open %s as %r" % (path, cls))
				return False
		h5file = None
		try:
			h5file = h5py.File(path, "r")
		except:
			return False
		has_particles = False
		for i in range(1,6):
			key = "/PartType%d" % particleType
			has_particles = has_particles or (key in h5file)
		return has_particles
			
	
	@classmethod
	def get_options(cls, path):
		return []
	
	@classmethod
	def option_to_args(cls, option):
		return []


dataset_type_map["gadget-hdf5"] = Hdf5MemoryMappedGadget

class InMemory(DatasetMemoryMapped):
	def __init__(self, name):
		super(InMemory, self).__init__(filename=None, nommap=True, name=name)


from numba import jit

@jit(nopython=True)
def reorder(array_from, array_temp, order):
	length = len(array_from)
	for i in range(length):
		array_temp[i] = array_from[order[i]]
	for i in range(length):
		array_from[i] = array_temp[i]

class SoneiraPeebles(InMemory):
	def __init__(self, dimension, eta, max_level, L):
		super(SoneiraPeebles, self).__init__(name="soneira-peebles")
		#InMemory.__init__(self)
		def todim(value):
			if isinstance(value, (tuple, list)):
				assert len(value) == dimension, "either a scalar or sequence of length equal to the dimension"
				return value
			else:
				return [value] * dimension

		eta = eta
		max_level = max_level
		N = eta**(max_level)
		# array[-1] is used as a temp storage
		array = np.zeros((dimension+1, N), dtype=np.float64)
		L = todim(L)

		for d in range(dimension):
			gavifast.soneira_peebles(array[d], 0, 1, L[d], eta, max_level)
		order = np.zeros(N, dtype=np.int64)
		gavifast.shuffled_sequence(order);
		for i, name in zip(range(dimension), "x y z w v u".split()):
			#np.take(array[i], order, out=array[i])
			reorder(array[i], array[-1], order)
			self.addColumn(name, array=array[i])

dataset_type_map["soneira-peebles"] = Hdf5MemoryMappedGadget


class Zeldovich(InMemory):
	def __init__(self, dim=2, N=256, n=-2.5, t=None, seed=None, name="zeldovich approximation"):
		super(Zeldovich, self).__init__(name=name)
		
		if seed is not None:
			np.random.seed(seed)
		#sys.exit(0)
		shape = (N,) * dim
		A = np.random.normal(0.0, 1.0, shape)
		F = np.fft.fftn(A) 
		K = np.fft.fftfreq(N, 1./(2*np.pi))[np.indices(shape)]
		k = (K**2).sum(axis=0)
		k_max = np.pi
		F *= np.where(np.sqrt(k) > k_max, 0, np.sqrt(k**n) * np.exp(-k*4.0))
		F.flat[0] = 0
		#pylab.imshow(np.where(sqrt(k) > k_max, 0, np.sqrt(k**-2)), interpolation='nearest')
		grf = np.fft.ifftn(F).real
		Q = np.indices(shape) / float(N-1) - 0.5
		s = np.array(np.gradient(grf)) / float(N)
		#pylab.imshow(s[1], interpolation='nearest')
		#pylab.show()
		s /= s.max() * 100.
		#X = np.zeros((4, 3, N, N, N))
		#for i in range(4):
		#if t is None:
		#	s = s/s.max()
		t = 1.
		X = Q + s * t

		for d, name in zip(range(dim), "xyzw"):
			self.addColumn(name, array=X[d].reshape(-1))
		for d, name in zip(range(dim), "xyzw"):
			self.addColumn("v"+name, array=s[d].reshape(-1))
		for d, name in zip(range(dim), "xyzw"):
			self.addColumn(name+"0", array=Q[d].reshape(-1))
		return
		
dataset_type_map["zeldovich"] = Zeldovich
		
		
import astropy.io.votable
class VOTable(DatasetMemoryMapped):
	def __init__(self, filename):
		super(VOTable, self).__init__(filename, nommap=True)
		table = astropy.io.votable.parse_single_table(filename)
		logger.debug("done parsing VO table")
		names = table.array.dtype.names
		
		data = table.array.data
		for i in range(len(data.dtype)):
			name = data.dtype.names[i]
			type = data.dtype[i]
			if type.kind in ["f", "i"]: # only store float and int
				#datagroup.create_dataset(name, data=table.array[name].astype(np.float64))
				#dataset.addMemoryColumn(name, table.array[name].astype(np.float64))
				self.addColumn(name, array=table.array[name])
		#dataset.samp_id = table_id
		#self.list.addDataset(dataset)
		#return dataset
	
	@classmethod
	def can_open(cls, path, *args):
		can_open = path.endswith(".vot")
		logger.debug("%r can open: %r"  %(cls.__name__, can_open))
		return can_open
dataset_type_map["votable"] = VOTable


class AsciiTable(DatasetMemoryMapped):
	def __init__(self, filename):
		super(AsciiTable, self).__init__(filename, nommap=True)
		import asciitable
		table = asciitable.read(filename)
		logger.debug("done parsing ascii table")
		#import pdb
		#pdb.set_trace()
		#names = table.array.dtype.names
		names = table.dtype.names
		
		#data = table.array.data
		for i in range(len(table.dtype)):
			name = table.dtype.names[i]
			type = table.dtype[i]
			if type.kind in ["f", "i"]: # only store float and int
				#datagroup.create_dataset(name, data=table.array[name].astype(np.float64))
				#dataset.addMemoryColumn(name, table.array[name].astype(np.float64))
				self.addColumn(name, array=table[name])
		#dataset.samp_id = table_id
		#self.list.addDataset(dataset)
		#return dataset
	
	@classmethod
	def can_open(cls, path, *args):
		can_open = path.endswith(".asc")
		logger.debug("%r can open: %r"  %(cls.__name__, can_open))
		return can_open
dataset_type_map["ascii"] = AsciiTable

class MemoryMappedGadget(DatasetMemoryMapped):
	def __init__(self, filename):
		super(MemoryMappedGadget, self).__init__(filename)
		#h5file = h5py.File(self.filename)
		import vaex.io.gadget
		length, posoffset, veloffset, header = vaex.io.gadget.getinfo(filename)
		self.addColumn("x", posoffset, length, dtype=np.float32, stride=3)
		self.addColumn("y", posoffset+4, length, dtype=np.float32, stride=3)
		self.addColumn("z", posoffset+8, length, dtype=np.float32, stride=3)
		
		self.addColumn("vx", veloffset, length, dtype=np.float32, stride=3)
		self.addColumn("vy", veloffset+4, length, dtype=np.float32, stride=3)
		self.addColumn("vz", veloffset+8, length, dtype=np.float32, stride=3)
dataset_type_map["gadget-plain"] = MemoryMappedGadget
		
		
def can_open(path, *args):
	for name, class_ in dataset_type_map.items():
		if class_.can_open(path, *args):
			return True
		
def load_file(path, *args):
	dataset_class = None
	for name, class_ in vaex.dataset.dataset_type_map.items():
		logger.debug("trying %r with class %r" % (path, class_))
		if class_.can_open(path, *args):
			logger.debug("can open!")
			dataset_class = class_
			break
	if dataset_class:
		dataset = dataset_class(path, *args)
		return dataset
	