import os
import re
import six
import array
import json
import pickle
import hashlib
import sysconfig
import time
import logging

import numpy as np
import ROOT
from rootpy import stl

import pax  # For version number
from pax import plugin, datastructure, exceptions
from pax.datastructure import make_event_proxy

ROOT.gROOT.SetBatch(True)
log = logging.getLogger('ROOTClass_helpers')

# Header for the automatically generated C++ code
# Must be separate from CLASS_TEMPLATE, since final file includes several classes
OVERALL_HEADER = """
#include "TFile.h"
#include "TTree.h"
#include "TObject.h"
#include "TString.h"
#include <vector>

"""

# Template for an autogenerated class (event or one of its children)
CLASS_TEMPLATE = """
{child_classes_code}

{ifndefs}
class {class_name} : public TObject {{

public:
{data_attributes}
    ClassDef({class_name}, {class_version});
}};

#endif

"""


class EncodeROOTClass(plugin.TransformPlugin):
    do_output_check = False

    def startup(self):
        self.config.setdefault('fields_to_ignore',
                               ('all_hits', 'raw_data', 'sum_waveforms', 'hits', 'pulses'))
        self._custom_types = []
        self.class_is_loaded = False
        self.last_collection = {}
        self.class_code = None

    def transform_event(self, event):
        if self.class_code is None:
            # Generate and load the pax event class code
            # Only master (in standalone mode) and processing_0 (in multiprocessing mode) are allowed to compile the lib
            # to avoid race conditions / clashes
            self.class_code = OVERALL_HEADER + self._build_model_class(event)
            load_event_class_code(self.class_code, self.config.get('lock_breaking_timeout'))

            if self.config['exclude_compilation_from_timer']:
                self.processor.timer.punch()

        root_event = ROOT.Event()
        self.set_root_object_attrs(event, root_event)
        self.last_collection = {}

        event_proxy = make_event_proxy(event, data=dict(root_event=pickle.dumps(root_event),
                                                        class_code=self.class_code))
        root_event.IsA().Destructor(root_event)
        return event_proxy

    def set_root_object_attrs(self, python_object, root_object):
        """Set attribute values of the root object based on data_model
        instance python object
        Returns nothing: modifies root_objetc in place
        """
        obj_name = python_object.__class__.__name__
        fields_to_ignore = self.config['fields_to_ignore']
        list_field_info = python_object.get_list_field_info()

        for field_name, field_value in python_object.get_fields_data():
            if field_name in fields_to_ignore:
                continue

            elif isinstance(field_value, list) or field_name in self.config['structured_array_fields']:
                # Collection field -- recursively initialize collection elements
                if field_name in self.config['structured_array_fields']:
                    # Convert the entries from numpy structured array to ordinary pax data models
                    element_model_name = self.config['structured_array_fields'][field_name]
                    pax_class = getattr(pax.datastructure, element_model_name)
                    pax_object_list = []
                    for h in field_value:
                        pax_object_list.append(pax_class(**{k: h[k] for k in field_value.dtype.names}))
                    field_value = pax_object_list

                else:
                    element_model_name = list_field_info[field_name].__name__

                root_vector = getattr(root_object, field_name)

                root_vector.clear()
                for element_python_object in field_value:
                    element_root_object = getattr(ROOT, element_model_name)()
                    self.set_root_object_attrs(element_python_object, element_root_object)
                    root_vector.push_back(element_root_object)
                self.last_collection[element_model_name] = field_value

            elif isinstance(field_value, np.ndarray):
                # Unfortunately we can't store numpy arrays directly into ROOT's ROOT.PyXXXBuffer.
                # Doing so will not give an error, but the data will be mangled!
                # Instead we have to use python's old array module...
                root_field = getattr(root_object, field_name)
                root_field_type = root_field.typecode
                if six.PY3:
                    root_field_type = root_field_type.decode("UTF-8")
                root_field_new = array.array(root_field_type, field_value.tolist())
                setattr(root_object, field_name, root_field_new)
            else:
                # Everything else apparently just works magically:
                setattr(root_object, field_name, field_value)

        # # Add values to user-defined fields
        for field_name, field_type, field_code in self.config['extra_fields'].get(obj_name, []):
            field = getattr(root_object, field_name)
            exec(field_code,
                 dict(root_object=root_object, python_object=python_object, field=field, self=self))

    def _get_index(self, py_object):
        """Return index of py_object in last collection of models of corresponding type seen in event"""
        return self.last_collection[py_object.__class__.__name__].index(py_object)

    def get_root_type(self, field_name, python_type):
        if field_name in self.config['force_types']:
            return self.config['force_types'][field_name]
        return self.config['type_mapping'][python_type]

    def _build_model_class(self, model):
        """Return ROOT C++ class definition corresponding to instance of data_model.Model
        """
        model_name = model.__class__.__name__
        self.log.debug('Building ROOT class for %s' % model_name)

        list_field_info = model.get_list_field_info()
        class_attributes = ''
        child_classes_code = ''
        for field_name, field_value in sorted(model.get_fields_data()):
            if field_name in self.config['fields_to_ignore']:
                continue

            # Collections (e.g. event.peaks)
            elif field_name in list_field_info or field_name in self.config['structured_array_fields']:
                if field_name in self.config['structured_array_fields']:
                    # Special handling for structure array fields.
                    # We need to convert these to 'ordinary' pax data models for storage (see set_root_object_attrs).
                    # field_value = [] makes sure the code below makes a new instance of the pax model
                    # rather than taking the first element of the array (which is a np.void object)
                    element_model_name = self.config['structured_array_fields'][field_name]
                    element_model = getattr(pax.datastructure, element_model_name)
                    field_value = []
                else:
                    element_model_name = list_field_info[field_name].__name__
                    element_model = list_field_info[field_name]
                self.log.debug("List column %s encountered. Type is %s" % (field_name, element_model_name))
                if element_model_name not in self._custom_types:
                    self._custom_types.append(element_model_name)
                    if not len(field_value):
                        # This event does not have an instance of the required type, we have to make one.
                        # TODO: These are some very ugly hacks!
                        # Ultimately this is due to the bad design choice of using a class instance rather than the
                        # class itself to make the root c++ class from. We have to pay this technical debt someday!
                        self.log.debug("Don't have a %s instance to use: making a fake one..." % element_model_name)
                        if element_model_name == 'Pulse':
                            # Pulse has a custom __init__ we need to obey... why did we do this again?
                            source = element_model(channel=0, left=0, right=0)
                        elif element_model_name == 'Peak':
                            # Peak has some array fields whose length depends on the configuration.
                            source = element_model()
                            n_channels = self.config['n_channels']
                            aargh = self.processor.config['BasicProperties.SumWaveformProperties']
                            n_waveform_samples = int(aargh['peak_waveform_length'] / self.config['sample_duration']) + 1
                            for _fn, length in ((
                                ('area_per_channel', n_channels),
                                ('hits_per_channel', n_channels),
                                ('coincidence_per_channel', n_channels),
                                ('tight_coincidence_thresholds', 5),
                                ('n_saturated_per_channel', n_channels),
                                ('sum_waveform', n_waveform_samples),
                                ('sum_waveform_top', n_waveform_samples),
                            )):
                                setattr(source, _fn, np.zeros(length, dtype=getattr(source, _fn).dtype))
                        else:
                            source = element_model()
                    else:
                        source = field_value[0]
                    child_classes_code += '\n' + self._build_model_class(source)
                class_attributes += '\tstd::vector <%s>  %s;\n' % (element_model_name, field_name)

            # Numpy array (assumed fixed-length, 1-d)
            elif isinstance(field_value, np.ndarray):
                class_attributes += '\t%s  %s[%d];\n' % (self.get_root_type(field_name,
                                                                            field_value.dtype.type.__name__),
                                                         field_name, len(field_value))

            # Everything else (int, float, bool)
            else:
                class_attributes += '\t%s  %s;\n' % (self.get_root_type(field_name,
                                                                        type(field_value).__name__),
                                                     field_name)

        # Add any user-defined extra fields
        for field_name, field_type, field_code in self.config['extra_fields'].get(model_name, []):
            class_attributes += '\t%s %s;\n' % (field_type, field_name)

        define = "#ifndef %s" % (model_name.upper() + "\n") + \
                 "#define %s " % (model_name.upper() + "\n")

        return CLASS_TEMPLATE.format(ifndefs=define,
                                     class_name=model_name,
                                     data_attributes=class_attributes,
                                     child_classes_code=child_classes_code,
                                     class_version=pax.__version__.replace('.', ''))


class WriteROOTClass(plugin.OutputPlugin):
    do_input_check = False
    do_output_check = False

    def startup(self):
        self.config.setdefault('buffer_size', 16000)
        self.config.setdefault('output_class_code', True)

        output_file = self.config['output_name'] + '.root'
        if os.path.exists(output_file):
            print("\n\nOutput file %s already exists, overwriting." % output_file)

        self.f = ROOT.TFile(output_file, "RECREATE")
        self.f.cd()
        self.tree_created = False

        # Write the metadata to the file as JSON
        ROOT.TNamed('pax_metadata', json.dumps(self.processor.get_metadata())).Write()

    def write_event(self, event_proxy):
        if not self.tree_created:
            # Load the event class code, ships with event_proxy
            load_event_class_code(event_proxy.data['class_code'], self.config.get('lock_breaking_timeout'))

            # Store the event class code in pax_event_class
            ROOT.TNamed('pax_event_class', event_proxy.data['class_code']).Write()

            # Make the event tree
            self.event_tree = ROOT.TTree(self.config['tree_name'],
                                         'Tree with %s events from pax' % self.config['tpc_name'])
            self.log.debug("Event class loaded, creating event")
            self.root_event = ROOT.Event()

            # TODO: does setting the splitlevel to 0 or 99 actually have an effect?
            self.event_tree.Branch('events', 'Event', self.root_event, self.config['buffer_size'], 99)
            self.tree_created = True

        # I haven't seen any documentation for the __assign__ thing... but it works :-)
        root_event = pickle.loads(event_proxy.data['root_event'])
        self.root_event.__assign__(root_event)
        self.event_tree.Fill()
        root_event.IsA().Destructor(root_event)

    def shutdown(self):
        if self.tree_created:
            self.f.cd()
            self.event_tree.Write()
            self.f.Close()


class ReadROOTClass(plugin.InputPlugin):
    def startup(self):
        if not os.path.exists(self.config['input_name']):
            raise ValueError("Input file %s does not exist" % self.config['input_name'])

        # Load the event class from the root file
        # This will fail on old format root files (before March 2016)
        load_pax_event_class_from_root(self.config['input_name'])

        # Make sure to store the ROOT file as an attribute
        # Else it will go out of scope => we die after next garbage collect
        self.f = ROOT.TFile(self.config['input_name'])

        self.t = self.f.Get(self.config['tree_name'])
        self.number_of_events = self.t.GetEntries()
        # TODO: read in event numbers, so we can select events!

    def get_events(self):
        for event_i in range(self.number_of_events):
            self.t.GetEntry(event_i)
            root_event = self.t.events
            event = datastructure.Event(n_channels=root_event.n_channels,
                                        start_time=root_event.start_time,
                                        sample_duration=root_event.sample_duration,
                                        stop_time=root_event.stop_time)
            self.set_python_object_attrs(root_event, event,
                                         self.config['fields_to_ignore'])
            yield event

    def set_python_object_attrs(self, root_object, py_object, fields_to_ignore):
        """Sets attribute values of py_object to corresponding values in root_object
        Returns nothing (modifies py_object in place)
        """
        for field_name, default_value in py_object.get_fields_data():

            if field_name in fields_to_ignore:
                continue

            try:
                root_value = getattr(root_object, field_name)
            except AttributeError:
                # Value not present in root object (e.g. event.all_hits)
                self.log.debug("%s not in root object?" % field_name)
                continue

            if field_name in self.config['structured_array_fields']:
                # Special case for hit fields
                # Convert from root objects to numpy array
                pax_class = getattr(datastructure, self.config['structured_array_fields'][field_name])
                dtype = pax_class.get_dtype()
                result = np.array([tuple([getattr(x, fn)
                                          for fn in dtype.names])
                                   for x in root_value], dtype=dtype)

            elif isinstance(default_value, list):
                child_class_name = py_object.get_list_field_info()[field_name].__name__
                result = []
                for child_i in range(len(root_value)):
                    child_py_object = getattr(datastructure, child_class_name)()
                    self.set_python_object_attrs(root_value[child_i],
                                                 child_py_object,
                                                 fields_to_ignore)
                    result.append(child_py_object)

            elif isinstance(default_value, np.ndarray):
                try:
                    if not len(root_value):
                        # Empty! no point to assign the value. Errors for
                        # structured array.
                        continue
                except TypeError:
                    self.log.warning("Strange error in numpy array field %s, "
                                     "type from ROOT object is %s, which is not"
                                     " iterable!" % (field_name,
                                                     type(root_value)))
                    continue
                # Use list() for same reason as described above in WriteROOTClass:
                # Something is wrong with letting the root buffers interact with
                # numpy arrays directly
                result = np.array(list(root_value), dtype=default_value.dtype)

            else:
                result = root_value

            setattr(py_object, field_name, result)


##
# Helper functions
# These are needed in several of the classes above and/or are exposed because they may be of use outside pax
##
def find_class_names(filename):
    """Return names of C++ classes declared in filename"""
    classnames = []
    with open(filename, 'r') as classfile:
        for line in classfile.readlines():
            m = re.match(r'class (\w*) ', line)
            if m:
                classnames.append(m.group(1))
    return classnames


def load_event_class_code(class_code, lock_breaking_timeout=None):
    """Load the pax event class contained in class_code.
    Computes checksum, writes to temporary file, then calls load_event_class
    """
    # Default is not in header, since this is called from several places with config.get()
    if lock_breaking_timeout is None:
        lock_breaking_timeout = 300
    checksum = hashlib.md5(class_code.encode()).hexdigest()
    class_filename = 'pax_event_class-%s.cpp' % checksum
    libfile = get_libname(class_filename)
    lockfile = get_libname(class_filename) + '.lock'
    we_made_the_lockfile = False

    def check_lock(pid_for_message='some other process'):
        """Check the existence of a lockfile, and return once it has been removed
        Breaks locks older than 90 seconds"""
        while os.path.exists(lockfile):
            age = time.time() - os.path.getmtime(lockfile)
            if age > lock_breaking_timeout:
                log.warning("Breaking lockfile %s as it is %d seconds old" % (lockfile, age))
                break
            log.debug("Waiting for %s to compile the pax event class." % pid_for_message)
            time.sleep(5)

    check_lock()

    if not os.path.exists(libfile):
        # Loading will trigger compilation
        # Create a lock file with our PID in it
        my_pid = os.getpid()
        we_made_the_lockfile = True
        with open(lockfile, mode='w') as lf:
            lf.write(str(my_pid))

        time.sleep(1)

        # Check the lock file to see if some other process overwrote it.
        with open(lockfile, mode='r') as lf:
            lockfile_pid = int(lf.read())

        if lockfile_pid != my_pid:
            # They did, ok... wait for them to compile it.
            we_made_the_lockfile = False
            check_lock(pid_for_message=lockfile_pid)
        else:
            # I am the chosen one! Output the cpp code and compile it
            with open(class_filename, mode='w') as outfile:
                outfile.write(class_code)

    load_event_class(os.path.abspath(class_filename))

    if we_made_the_lockfile:
        os.remove(lockfile)


def load_event_class(filename, force_recompile=False):
    """Read a C++ root class definition from filename, generating dictionaries for vectors of classes
    Or, if the library hass been compiled before, load it.
    """
    # Load , or if necessary compile, the file in ROOT
    # Unfortunately this must happen in the current directory.
    # I tried rootpy.register_file, but it is very weird: compilation is triggered only when you make an object
    # I then tried to make a object right after, but get a weird segfault later... don't we all just love C++...
    libname = get_libname(filename)
    if os.path.exists(libname) and not force_recompile:
        if ROOT.gSystem.Load(libname) not in (0, 1):
            raise RuntimeError("failed to load the library '{0}'".format(libname))
    else:
        ROOT.gROOT.ProcessLine('.L %s+' % filename)

    # Generate dictionaries for vectors of custom classes
    # If you don't do this, the vectors will actually be useless sterile root objects
    for name in find_class_names(filename):
        stl.generate("std::vector<%s>" % name, ['<vector>', "%s" % filename], True)


def get_libname(cppname):
    """Returns name of cpp file that would be obtained after compiling"""
    return os.path.splitext(cppname)[0] + "_cpp" + sysconfig.get_config_var('SO' if six.PY2 else 'SHLIB_SUFFIX')


def load_pax_event_class_from_root(rootfilename):
    """Load the pax event class from the pax root file rootfilename"""
    # Open the ROOT file just to get the pax class code out
    # We want to suppress the "help me I don't have the right dictionaries" warnings,
    # since we want to load the class code to solve this very problem!
    with ShutUpROOT():
        f = ROOT.TFile(rootfilename)
    if 'pax_event_class' not in [x.GetName() for x in list(f.GetListOfKeys())]:
        raise exceptions.MaybeOldFormatException("Root file %s does not contain pax event class code.\n "
                                                 "Maybe it was made before March 2016? See #323." % rootfilename)
    load_event_class_code(f.Get('pax_event_class').GetTitle())
    f.Close()


class ShutUpROOT:
    """Context manager to temporarily suppress ROOT warnings
    Stolen from https://root.cern.ch/phpBB3/viewtopic.php?f=14&t=18096
    """
    def __init__(self, level=ROOT.kFatal):
        self.level = level

    def __enter__(self):
        self.oldlevel = ROOT.gErrorIgnoreLevel
        ROOT.gErrorIgnoreLevel = self.level

    def __exit__(self, type, value, traceback):
        ROOT.gErrorIgnoreLevel = self.oldlevel
