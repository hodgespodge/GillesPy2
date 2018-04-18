import gillespy2
from .gillespySolver import GillesPySolver
import os #for getting directories for C++ files
import shutil #for deleting/copying files
import subprocess #For calling make and executing c solver
import inspect #for finding the Gillespy2 module path
import tempfile #for temporary directories
import numpy as np
import math

GILLESPY_PATH = os.path.dirname(inspect.getfile(gillespy2))
GILLESPY_C_DIRECTORY = os.path.join(GILLESPY_PATH, 'c_base/')

#TODO:
#    Create constructor for PyCSolver which sets up directories
#    Allow C Solver to take command line args for number_trajectories, number_timesteps, end_time
#    Allow PyCSolver to run without recompiling C
#    Write up results motivation/summary (2 paragraphs) with plots for poster

def copy_files(destination):
    src_files = os.listdir(GILLESPY_C_DIRECTORY)
    for src_file in src_files:
        src_file = os.path.join(GILLESPY_C_DIRECTORY, src_file)
        if os.path.isfile(src_file):
            shutil.copy(src_file, destination)


def write_constants(outfile, model, reactions, species):
    #Write mandatory constants
    outfile.write("const double vol = {};\n".format(model.volume))
    outfile.write("std :: string s_names[] = {");
    if len(species) > 0:
        #Write model species names.
        for i in range(len(species)-1):
            outfile.write('"{}", '.format(species[i]))
        outfile.write('"{}"'.format(species[-1]))
        outfile.write("};\nuint populations[] = {")
        #Write initial populations.
        for i in range(len(species)-1):
            outfile.write('{}, '.format(model.listOfSpecies[species[i]].initial_value))
        outfile.write('{}'.format(model.listOfSpecies[species[-1]].initial_value))
        outfile.write("};\n")
    if len(reactions) > 0:
        #Write reaction names
        outfile.write("std :: string r_names[] = {")
        for i in range(len(reactions)-1):
            outfile.write('"{}", '.format(reactions[i]))
        outfile.write('"{}"'.format(reactions[-1]))
        outfile.write("};\n")
    for param in model.listOfParameters:
        outfile.write("const double {0} = {1};\n".format(param, model.listOfParameters[param].value))


def write_propensity(outfile, model, reactions, species):
    for i in range(len(reactions)):
        propensity_function = model.listOfReactions[reactions[i]].propensity_function
        #Replace species references with array references
        for j in range(len(species)):
            propensity_function = propensity_function.replace(species[j], "state[{}]".format(j))
        #Write switch statement case for reaction
        outfile.write("""
        case {0}:
            return {1};
        """.format(i, propensity_function))


def write_reactions(outfile, model, reactions, species):
    for i in range(len(reactions)):
        reaction = model.listOfReactions[reactions[i]]
        for j in range(len(species)):
            change = (reaction.products.get(model.listOfSpecies[species[j]], 0)) - (reaction.reactants.get(model.listOfSpecies[species[j]], 0))
            if change != 0:
                outfile.write("model.reactions[{0}].species_change[{1}] = {2};\n".format(i, j, change))


def parse_output(results, number_of_trajectories, number_timesteps, number_species):
    trajectory_base = np.empty((number_of_trajectories, number_timesteps, number_species+1))
    for timestep in range(number_timesteps):
        values = results[timestep].split(" ")
        trajectory_base[:, timestep, 0] = float(values[0])
        index = 1
        for trajectory in range(number_of_trajectories):
            for species in range(number_species):
                trajectory_base[trajectory, timestep, 1 + species] = float(values[index+species])
            index += number_species
    return trajectory_base


def parse_binary_output(results_buffer, number_of_trajectories, number_timesteps, number_species):
    trajectory_base = np.empty((number_of_trajectories, number_timesteps, number_species+1))
    step_size = number_species * number_of_trajectories + 1 #1 for timestep
    data = np.frombuffer(results_buffer, dtype=np.float64)
    assert(len(data) == (number_of_trajectories*number_timesteps*number_species + number_timesteps))
    for timestep in range(number_timesteps):
        index = step_size * timestep
        trajectory_base[:, timestep, 0] = data[index]
        index += 1
        for trajectory in range(number_of_trajectories):
            for species in range(number_species):
                trajectory_base[trajectory, timestep, 1 + species] = data[index + species]
            index += number_species
    return trajectory_base

class SSACSolver(GillesPySolver):
    """TODO"""
    def __init__(self, model, output_directory=None, delete_directory=True):
        super(SSACSolver, self).__init__()
        self.compiled = False
        self.model = model
        #Create constant, ordered lists for reactions/species
        self.reactions = list(self.model.listOfReactions.keys())
        self.species = list(self.model.listOfSpecies.keys())
        self.delete_directory = delete_directory
        
        if isinstance(output_directory, str):
            output_directory = os.path.abspath(output_directory)
            
        if isinstance(output_directory, str) and not os.path.isfile(output_directory):
            self.output_directory = output_directory
            if not os.path.isdir(output_directory):
                #set up directory if needed
                os.makedirs(self.output_directory)
        else:
            #Set up temporary directory
            self.temporary_directory = tempfile.TemporaryDirectory()
            self.delete_directory = True
            self.output_directory = self.temporary_directory.name
        #copy files to directory
        copy_files(self.output_directory)
        #write template file
        self.write_template()
        #compile file
        self.compile()
        
    def __del__(self):
        if self.delete_directory and os.path.isdir(self.output_directory):
            shutil.rmtree(self.output_directory)
        
    def write_template(self, template_file='SimulationTemplate.cpp'):
        #Open up template file for reading.
        with open(os.path.join(self.output_directory, template_file), 'r') as template:
            #Write simulation C++ file.
            template_keyword = "__DEFINE_"
            #Use same lists of model's species and reactions to maintain order
            with open(os.path.join(self.output_directory, 'UserSimulation.cpp'), 'w') as outfile:
                for line in template:
                    if line.startswith(template_keyword):
                        line = line[len(template_keyword):]
                        if line.startswith("CONSTANTS"):
                            write_constants(outfile, self.model, self.reactions, self.species)
                        if line.startswith("PROPENSITY"):
                            write_propensity(outfile, self.model, self.reactions, self.species)
                        if line.startswith("REACTIONS"):
                            write_reactions(outfile, self.model, self.reactions, self.species)
                    else:
                        outfile.write(line)
    def compile(self):
        #Use makefile.
        cleaned = subprocess.run(["make", "-C", self.output_directory, 'cleanSimulation'], stdout=subprocess.PIPE)
        built = subprocess.run(["make", "-C", self.output_directory, 'UserSimulation'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        #Use makefile.        
        if built.returncode == 0:
            self.compiled = True
        else:
            print("Error encountered while compiling file:\nReturn code: {0}.\nError:\n{1}\n".format(built.returncode, built.stderr))
            
    def run(self, model, t=20, number_of_trajectories=1,
            increment=0.05, seed=None, debug=False, show_labels=False, stochkit_home=None):
        self.simulation_data = None
        number_timesteps = int(t//increment + 1)
        if self.compiled:
            #Execute simulation.
            args = [os.path.join(self.output_directory, 'UserSimulation'), '-trajectories', str(number_of_trajectories), '-timesteps', str(number_timesteps), '-end', str(t)]
            if isinstance(seed, int):
                args.append('-seed')
                args.append(str(seed))
                
            simulation = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            #Parse/return results.
            if simulation.returncode == 0:
                trajectory_base = parse_binary_output(simulation.stdout, number_of_trajectories, number_timesteps, len(self.species))
                #Format results
                if show_labels:
                    self.simulation_data = []
                    for trajectory in range(number_of_trajectories):
                        data = {}
                        data['time'] = trajectory_base[trajectory,:,0]
                        for i in range(len(self.species)):
                            data[self.species[i]] = trajectory_base[trajectory, :, i]
                        self.simulation_data.append(data)
                else:
                    self.simulation_data = trajectory_base
            else:
                print("Error encountered while running simulation C++ file:\nReturn code: {0}.\nError:\n{1}\n".format(simulation.returncode, simulation.stderr))
        return self.simulation_data

