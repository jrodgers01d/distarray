"""
Create a fake 3D array that will pretend to be a seismic volume.

This is not particularly efficient, especially for large files.
"""

from math import exp
import numpy, numpy.random
import h5py

# Physical size of volume.
PHYSICAL_SIZE = (10.0, 10.0, 20.0)

# Array size of volume.
#ARRAY_SIZE = (2, 2, 10)
ARRAY_SIZE = (4, 4, 25)
#ARRAY_SIZE = (512, 512, 1024)
#ARRAY_SIZE = (512, 512, 2048)

def g(z, z0, A=100.0, C=2.5, mu=0.1):
    '''Gaussian.  Put a peak at depth z=z0.'''
    g = A * numpy.exp(-mu * (z - z0)**2) + C
    return g

def p(x, y):
    '''Get the depth on the plane where we want to put the peak.'''
    nx, ny, nz = 0.1, -0.2, 1.0
    D = 0.5 * PHYSICAL_SIZE[2]
    # Depth of peak.
    z0 = (D - nx * x - ny * y) / nz
    return z0

def get_physical(index, dimension):
    ''' Convert array index into physical value.'''
    return PHYSICAL_SIZE[dimension] * float(index) / float(ARRAY_SIZE[dimension])

def get_x(i):
    return get_physical(i, 0)

def get_y(j):
    return get_physical(j, 1)

def get_z(k):
    return get_physical(k, 2)

def create_horizon():
    '''Get the horizon surface where we will place the peak.'''
    horizon = numpy.zeros((ARRAY_SIZE[0], ARRAY_SIZE[1]))
    for i in xrange(ARRAY_SIZE[0]):
        x = get_x(i)
        for j in xrange(ARRAY_SIZE[1]):
            y = get_y(j)
            horizon[i, j] = p(x, y)
    return horizon

def create_volume():
    print 'Creating volume array...'
    # No randomness right now...
    #vol = numpy.random.randn(SIZE[0], SIZE[1], SIZE[2])
    vol = numpy.zeros(ARRAY_SIZE, dtype=numpy.float32)
    print 'Creating horizon...'
    horizon = create_horizon()
    print 'Filling array...'
    z = numpy.empty((ARRAY_SIZE[2],))
    for k in xrange(ARRAY_SIZE[2]):
        z[k] = get_z(k)
    for i in xrange(ARRAY_SIZE[0]):
        print 'Index', i, 'of', ARRAY_SIZE[0]
        for j in xrange(ARRAY_SIZE[1]):
            z0 = horizon[i, j]
            vol[i, j, :] = g(z, z0)
    print 'Done.'
    return vol

def create_file(volume, filename, key):
    '''Create an HDF5 file with the seismic volume.'''
    f = h5py.File(filename, 'w')
    dataset = f.create_dataset(key, ARRAY_SIZE, dtype='f')
    dataset[...] = volume
    print "Dataset dataspace is", dataset.shape
    print "Dataset Numpy datatype is", dataset.dtype
    print "Dataset name is", dataset.name
    print "Dataset is a member of the group", dataset.parent
    print "Dataset was created in the file", dataset.file
    f.close()

def main():
    filename = 'seismic.hdf5'
    key = 'seismic'
    vol = create_volume()
    if False:
        print vol
    create_file(vol, filename=filename, key=key)

if __name__ == '__main__':
    main()
