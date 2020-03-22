#!/usr/bin/env python3

from autokernel.kconfig import *
from autokernel.node_detector import NodeDetector
from autokernel.lkddb import Lkddb
from autokernel.config import load_config
from autokernel import log

import argparse
import gzip
import os
import shutil
import subprocess
import sys
import tempfile
import kconfiglib
from kconfiglib import STR_TO_TRI, TRI_TO_STR
from datetime import datetime, timezone
from pathlib import Path

def die(message):
    log.error(message)
    sys.exit(1)

def has_proc_config_gz():
    """
    Checks if /proc/config.gz exists
    """
    return os.path.isfile("/proc/config.gz")

def unpack_proc_config_gz():
    """
    Unpacks /proc/config.gz into a temporary file
    """
    tmp = tempfile.NamedTemporaryFile()
    with gzip.open("/proc/config.gz", "rb") as f:
        shutil.copyfileobj(f, tmp)
    return tmp

def kconfig_load_file_or_current_config(kconfig, config_file):
    """
    Applies the given kernel config file to kconfig, or uses /proc/config.gz if config_file is None.
    """

    if config_file:
        log.info("Applying kernel config from '{}'".format(config_file))
        kconfig.load_config(config_file)
    else:
        log.info("Applying kernel config from '/proc/config.gz'")
        with unpack_proc_config_gz() as tmp:
            kconfig.load_config(tmp.name)

def generated_by_autokernel_header():
    return "# Generated by autokernel on {}\n".format(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"))

def vim_config_modeline_header():
    return "# vim: set ft=sh:\n"

def apply_autokernel_config(kconfig, config):
    """
    Applies the given autokernel configuration to a freshly loaded kconfig object,
    and returns the kconfig and a dictionary of changes
    """
    log.info("Applying autokernel configuration")

    def value_to_str(value):
        if value in STR_TO_TRI:
            return '[{}]'.format(value)
        else:
            return "'{}'".format(value)

    # Track all changed symbols and values.
    changes = {}

    # Asserts that the symbol has the given value
    def assert_symbol(symbol, value):
        # TODO differentiate between m and y if user wants that!
        sym = kconfig.syms[symbol]
        if sym.str_value != value:
            die("Assertion failed: {} should be {} but is {}".format(symbol, value_to_str(value), value_to_str(sym.str_value)))

    # Sets a symbols value if and asserts that there are no conflicting double assignments
    def set_symbol(symbol, value):
        # If the symbol was changed previously
        if symbol in changes:
            # Assert that it is changed to the same value again
            if changes[symbol][1] != value:
                die("Conflicting change for symbol {} (previously set to {}, now {})".format(symbol, value_to_str(changes[symbol][1]), value_to_str(value)))

            # And skip the reassignment
            return

        # Get the kconfig symbol, and change the value
        sym = kconfig.syms[symbol]
        original_value = sym.str_value
        if not sym.set_value(value):
            die("Invalid value {} for symbol {}".format(value_to_str(value), symbol))

        if sym.str_value != value:
            log.warn("Symbol assignment failed: {} from {} -> {}".format(symbol, value_to_str(sym.str_value), value_to_str(value)))

        # Track the change
        if original_value != sym.str_value:
            changes[symbol] = (original_value, sym.str_value)
            log.verbose("{} : {}".format(value_to_str(sym.str_value), symbol))

    # Visit all module nodes and apply configuration changes
    visited = set()
    def visit(module):
        # Ensure we visit only once
        if module.name in visited:
            return
        visited.add(module.name)

        # Ensure all dependencies are processed first
        for d in module.dependencies:
            visit(d)

        # Merge all given kconf files of the module
        for filename in module.merge_kconf_files:
            log.verbose("Merging external kconf '{}'".format(filename))
            kconfig.load_config(filename)

        # Process all symbol value changes
        for symbol, value in module.assignments:
            set_symbol(symbol, value)

        # Process all assertions
        for symbol, value in module.assertions:
            assert_symbol(symbol, value)

    # Visit the root node and apply all symbol changes
    visit(config.kernel.module)
    log.info("Changed {} symbols".format(len(changes)))

    return changes

def main_check_config(args):
    """
    Main function for the 'check' command.
    """
    if args.compare_config:
        log.info("Checking generated config against '{}'".format(args.compare_config))
    else:
        if not has_proc_config_gz():
            die("This kernel does not expose /proc/config.gz. Please provide the path to a valid config file manually.")
        log.info("Checking generated config against currently running kernel")

    # Load configuration file
    config = load_config(args.autokernel_config)

    # Load symbols from Kconfig
    kconfig_gen = load_kconfig(args.kernel_dir)
    # Apply autokernel configuration
    changes = apply_autokernel_config(kconfig_gen, config)

    # Load symbols from Kconfig
    kconfig_cmp = load_kconfig(args.kernel_dir)
    # Load the given config file or the current kernel's config
    kconfig_load_file_or_current_config(kconfig_cmp, args.compare_config)

    for sym in kconfig_gen.syms:
        sym_gen = kconfig_gen.syms[sym]
        sym_cmp = kconfig_cmp.syms[sym]
        if sym_gen.str_value != sym_cmp.str_value:
            print("[{} -> {}] {}".format(sym_cmp.str_value, sym_gen.str_value, sym))

def main_generate_config(args, config=None):
    """
    Main function for the 'generate_config' command.
    """
    log.info("Generating kernel configuration")
    if not config:
        # Load configuration file
        config = load_config(args.autokernel_config)

    # Fallback for config output
    if not hasattr(args, 'output') or not args.output:
        args.output = os.path.join(args.kernel_dir, '.config')

    # Load symbols from Kconfig
    kconfig = load_kconfig(args.kernel_dir)
    # Apply autokernel configuration
    apply_autokernel_config(kconfig, config)

    # Write configuration to file
    kconfig.write_config(
            filename=args.output,
            header=generated_by_autokernel_header(),
            save_old=False)

    log.info("Configuration written to '{}'".format(args.output))

def build_kernel(args, config, pass_id):
    if pass_id == 'initial':
        log.info("Building kernel")
    elif pass_id == 'pack':
        log.info("Rebuilding kernel to pack external resources")
    else:
        raise ValueError("pass_id has an invalid value '{}'".format(pass_id))

    # TODO cleaning capabilities?
    if subprocess.run(['make'], cwd=args.kernel_dir).returncode != 0:
        die("make failed in {}".format(args.kernel_dir))

def build_initramfs(args, config):
    log.info("Building initramfs")

    # TODO don't build initramfs if not needed
    print("subprocess.run(['genkernel'], cwd={})".format(args.kernel_dir))

def main_build(args, config=None):
    """
    Main function for the 'build' command.
    """
    if not config:
        # Load configuration file
        config = load_config(args.autokernel_config)

    generate_config(args, config)

    # Build the kernel
    build_kernel(args, config, pass_id='initial')

    # Build the initramfs, if enabled
    if config.build.enable_initramfs:
        build_initramfs(args, config)

        # Pack the initramfs into the kernel if desired
        if config.build.pack['initramfs']:
            build_kernel(args, config, pass_id='pack')

def install_kernel(args, config):
    log.info("Installing kernel")

    print(str(config.install.target_dir))
    print(str(config.install.target).replace('{KV}', 'KERNELVERSION'))

def install_initramfs(args, config):
    # TODO dont install initramfs if not needed (section not given)
    log.info("Installing initramfs")

def main_install(args, config=None):
    """
    Main function for the 'install' command.
    """
    if not config:
        # Load configuration file
        config = load_config(args.autokernel_config)

    # Mount
    for i in config.install.mount:
        if not os.access(i, os.R_OK):
            die("Permission denied on accessing '{}'. Aborting.".format(i))

        if not os.path.ismount(i):
            if subprocess.run(['mount', '--', i]).returncode != 0:
                die("Could not mount '{}'. Aborting.".format(i))

    # Check mounts
    for i in config.install.mount + config.install.assert_mounted:
        if not os.access(i, os.R_OK):
            die("Permission denied on accessing '{}'. Aborting.".format(i))

        if not os.path.ismount(i):
            die("'{}' is not mounted. Aborting.".format(i))

    install_kernel(args, config)

    # Install the initramfs, if enabled and not packed
    if config.build.enable_initramfs and not config.build.pack['initramfs']:
        install_initramfs(args, config)

def main_build_all(args):
    """
    Main function for the 'all' command.
    """
    log.info("Started full build")
    # Load configuration file
    config = load_config(args.autokernel_config)

    main_build(args, config)
    main_install(args, config)

class Module():
    """
    A module consists of dependencies (other modules) and option assignments.
    """
    def __init__(self, name):
        self.name = name
        self.deps = []
        self.assignments = []
        self.assertions = []
        self.rev_deps = []

def check_config_against_detected_modules(kconfig, modules):
    log.info("Here are the detected options with both current and desired value.")
    log.info("The output format is: [current] OPTION_NAME = desired")
    log.info("HINT: Options are ordered by dependencies, i.e. applying")
    log.info("      them from top to buttom will work")
    log.info("Detected options:")

    visited = set()
    visited_opts = set()
    color = {
        NO: "[1;31m",
        MOD: "[1;33m",
        YES: "[1;32m",
    }

    def visit_opt(opt, v):
        from autokernel.constants import NO, MOD, YES

        # Ensure we visit only once
        if opt in visited_opts:
            return
        visited_opts.add(opt)

        sym = kconfig.syms[opt]
        if v in STR_TO_TRI:
            sym_v = sym.tri_value
            tri_v = STR_TO_TRI[v]

            if tri_v == sym_v:
                # Match
                v_color = color[YES]
            elif tri_to_bool(tri_v) == tri_to_bool(sym_v):
                # Match, but mixed y and m
                v_color = color[MOD]
            else:
                # Mismatch
                v_color = color[NO]

            # Print option value
            print("[{}{}[m] {} = {}".format(v_color, TRI_TO_STR[sym_v], sym.name, v))
        else:
            # Print option assignment
            print("{} = {}{}[m".format(sym.name, color[YES] if sym.str_value == v else color[NO], sym.str_value))

    def visit(m):
        # Ensure we visit only once
        if m in visited:
            return
        visited.add(m)

        # First visit all dependencies
        for d in m.deps:
            visit(d)
        # Then print all assignments
        for a, v in m.assignments:
            visit_opt(a, v)

    # Visit all modules
    for m in modules:
        visit(modules[m])

class KernelConfigWriter:
    """
    Writes modules to the given file in kernel config format.
    """
    def __init__(self, file):
        self.file = file
        self.file.write(generated_by_autokernel_header())
        self.file.write(vim_config_modeline_header())

    def write_module(self, module):
        if len(module.assignments) == len(module.assertions) == 0:
            return

        content = ""
        for d in module.rev_deps:
            content += "# required by {}\n".format(d.name)
        content += "# module {}\n".format(module.name)
        for a, v in module.assignments:
            if v in "nmy":
                content += "CONFIG_{}={}\n".format(a, v)
            else:
                content += "CONFIG_{}=\"{}\"\n".format(a, v)
        for o in module.assertions:
            content += "# REQUIRES {}\n".format(o)
        self.file.write(content)

class ModuleConfigWriter:
    """
    Writes modules to the given file in the module config format.
    """
    def __init__(self, file):
        self.file = file
        self.file.write(generated_by_autokernel_header())
        self.file.write(vim_config_modeline_header())

    def write_module(self, module):
        content = ""
        for d in module.rev_deps:
            content += "# required by {}\n".format(d.name)
        content += "module {} {{\n".format(module.name)
        for d in module.deps:
            content += "\tuse {};\n".format(d.name)
        for a, v in module.assignments:
            content += "\tset {} {};\n".format(a, v)
        for o in module.assertions:
            content += "\tassert {};\n".format(o)
        content += "}\n\n"
        self.file.write(content)

class ModuleCreator:
    def __init__(self):
        self.modules = {}
        self.module_for_sym = {}
        self.module_select_all = Module('module_select_all')

    def _create_reverse_deps(self):
        # Clear rev_deps
        for m in self.modules:
            self.modules[m].rev_deps = []
        self.module_select_all.rev_deps = []

        # Fill in reverse dependencies for all modules
        for m in self.modules:
            for d in self.modules[m].deps:
                d.rev_deps.append(self.modules[m])

        # Fill in reverse dependencies for select_all module
        for d in self.module_select_all.deps:
            d.rev_deps.append(self.module_select_all)

    def _add_module_for_option(self, sym):
        """
        Recursively adds a module for the given option,
        until all dependencies are satisfied.
        """
        mod = Module("config_{}".format(sym.name.lower()))
        mod.assignments.append((sym.name, 'y'))

        # Only process dependencies, if they are not already satisfied
        if not expr_value(sym.direct_dep):
            for d, v in required_deps(sym):
                if v:
                    depm = self.add_module_for_sym(d)
                    mod.deps.append(depm)
                else:
                    mod.assignments.append((d.name, 'n'))

        self.modules[mod.name] = mod
        return mod

    def add_module_for_sym(self, sym):
        """
        Adds a module for the given symbol (and its dependencies).
        """
        if sym in self.module_for_sym:
            return self.module_for_sym[sym]

        # Create a module for the symbol, if it doesn't exist already
        mod = self._add_module_for_option(sym)
        self.module_for_sym[sym] = mod
        return mod

    def select_module(self, mod):
        self.module_select_all.deps.append(mod)

    def add_external_module(self, mod):
        self.modules[mod.name] = mod

    def _write_detected_modules(self, f, output_type, output_module_name):
        """
        Writes the collected modules to a file / stdout, in the requested output format.
        """
        if output_type == 'kconf':
            writer = KernelConfigWriter(f)
        elif output_type == 'module':
            writer = ModuleConfigWriter(f)
        else:
            die("Invalid output_type '{}'".format(output_type))

        # Fill in reverse dependencies for all modules
        self._create_reverse_deps()

        visited = set()
        def visit(m):
            # Ensure we visit only once
            if m in visited:
                return
            visited.add(m)
            writer.write_module(m)

        # Write all modules in topological order
        for m in self.modules:
            visit(self.modules[m])

        # Lastly, write "select_all" module, if it has been used
        if len(self.module_select_all.deps) > 0:
            self.module_select_all.name = output_module_name
            writer.write_module(self.module_select_all)

    def write_detected_modules(self, output, output_type, output_module_name):
        # Write all modules in the given format to the given output file / stdout
        if output:
            try:
                with open(output, 'w') as f:
                    self._write_detected_modules(f, output_type, output_module_name)
                    log.info("Module configuration written to '{}'".format(output))
            except IOError as e:
                die(str(e))
        else:
            self._write_detected_modules(sys.stdout, output_type, output_module_name)

def detect_modules(kconfig):
    """
    Detects required options for the current system organized into modules.
    Any option with dependencies will also be represented as a module. It returns
    a dict which maps module names to the module objects. The special module returned
    additionaly is the module which selects all detected modules as dependencies.
    """
    log.info("Detecting kernel configuration for local system")
    log.info("HINT: It might be beneficial to run this while using a very generic")
    log.info("      and modular kernel, such as the default kernel on Arch Linux.")

    local_module_count = 0
    def next_local_module_id():
        """
        Returns the next id for a local module
        """
        nonlocal local_module_count
        i = local_module_count
        local_module_count += 1
        return i

    module_creator = ModuleCreator()
    def add_module_for_detected_node(node, opts):
        """
        Adds a module for the given detected node
        """
        mod = Module("{:04d}_{}".format(next_local_module_id(), node.get_canonical_name()))
        for o in opts:
            sym = kconfig.syms[o]
            m = module_creator.add_module_for_sym(sym)
            mod.deps.append(m)
        module_creator.add_external_module(mod)
        return mod

    # Load the configuration database
    config_db = Lkddb()
    # Inspect the current system
    detector = NodeDetector()

    # Try to find detected nodes in the database
    log.info("Matching detected nodes against database")

    # First sort all nodes for more consistent output between runs
    all_nodes = []
    # Find options in database for each detected node
    for detector_node in detector.nodes:
        all_nodes.extend(detector_node.nodes)
    all_nodes.sort(key=lambda x: x.get_canonical_name())

    for node in all_nodes:
        opts = config_db.find_options(node)
        if len(opts) > 0:
            # If there are options for the node in the database,
            # add a module for the detected node and its options
            mod = add_module_for_detected_node(node, opts)
            # Select the module in the global selector module
            module_creator.select_module(mod)

    return module_creator

def main_detect(args):
    """
    Main function for the 'main_detect' command.
    """
    # Check if we should write a config or report differences
    check_only = args.check_config is not 0

    # Assert that --check is not used together with --type
    if check_only and args.output_type:
        die("--check and --type are mutually exclusive")

    # Assert that --check is not used together with --output
    if check_only and args.output:
        die("--check and --output are mutually exclusive")

    # Determine the config file to check against, if applicable.
    if check_only:
        if args.check_config:
            log.info("Checking generated config against '{}'".format(args.check_config))
        else:
            if not has_proc_config_gz():
                die("This kernel does not expose /proc/config.gz. Please provide the path to a valid config file manually.")
            log.info("Checking generated config against currently running kernel")

    # Load symbols from Kconfig
    kconfig = load_kconfig(args.kernel_dir)
    # Detect system nodes and create modules
    module_creator = detect_modules(kconfig)

    if check_only:
        # Load the given config file or the current kernel's config
        kconfig_load_file_or_current_config(kconfig, args.check_config)
        # Check all detected symbols' values and report them
        check_config_against_detected_modules(kconfig, module_creator.modules)
    else:
        # Add fallback for output type.
        if not args.output_type:
            args.output_type = 'module'

        # Allow - as an alias for stdout
        if args.output == '-':
            args.output = None

        # Write all modules in the given format to the given output file / stdout
        module_creator.write_detected_modules(args.output, args.output_type, args.output_module_name)

def main_search(args):
    """
    Main function for the 'search' command.
    """
    # Load symbols from Kconfig
    kconfig = load_kconfig(args.kernel_dir)

    for config_symbol in args.config_symbols:
        # Get symbol
        if config_symbol.startswith('CONFIG_'):
            sym_name = config_symbol[len('CONFIG_'):]
        else:
            sym_name = config_symbol

        # Print symbol
        sym = kconfig.syms[sym_name]
        print(sym)

def main_deps(args):
    """
    Main function for the 'search' command.
    """
    # Load symbols from Kconfig
    kconfig = load_kconfig(args.kernel_dir)

    # Get symbol
    if args.config_symbol.startswith('CONFIG_'):
        sym_name = args.config_symbol[len('CONFIG_'):]
    else:
        sym_name = args.config_symbol
    sym = kconfig.syms[sym_name]

    # Apply autokernel configuration only if we want our dependencies based on the current configuration
    if not args.dep_global:
        # Load configuration file
        config = load_config(args.autokernel_config)
        # Apply kernel config
        apply_autokernel_config(kconfig, config)

    # Create a module for the detected option
    module_creator = ModuleCreator()
    module_creator.add_module_for_sym(sym)

    # Add fallback for output type.
    if not args.output_type:
        args.output_type = 'module'

    # Allow - as an alias for stdout
    if args.output == '-':
        args.output = None

    # Write the module
    module_creator.write_detected_modules(args.output, args.output_type, "ERROR_PLEASE_REPORT_TO_DEVELOPERS")

def check_file_exists(value):
    """
    Checks if the given exists
    """
    if not os.path.isfile(value):
        raise argparse.ArgumentTypeError("'{}' is not a file".format(value))
    return value

def check_kernel_dir(value):
    """
    Checks if the given value is a valid kernel directory path.
    """
    if not os.path.isdir(value):
        raise argparse.ArgumentTypeError("'{}' is not a directory".format(value))

    if not os.path.exists(os.path.join(value, 'Kconfig')):
        raise argparse.ArgumentTypeError("'{}' is not a valid kernel directory, as it does not contain a Kconfig file".format(value))

    return value

class ArgumentParserError(Exception):
    pass

class ThrowingArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ArgumentParserError(message)

def main():
    """
    Parses options and dispatches control to the correct subcommand function
    """
    parser = ThrowingArgumentParser(description="TODO. If no mode is given, 'autokernel all' will be executed.")
    subparsers = parser.add_subparsers(title="commands",
            description="Use 'autokernel command --help' to view the help for any command.",
            metavar='command')

    # General options
    parser.add_argument('-k', '--kernel-dir', dest='kernel_dir', default='/usr/src/linux', type=check_kernel_dir,
            help="The kernel directory to operate on. The default is /usr/src/linux.")
    parser.add_argument('-C', '--config', dest='autokernel_config', default='/etc/autokernel/autokernel.conf', type=check_file_exists,
            help="The autokernel configuration file to use. The default is '/etc/autokernel/autokernel.conf'.")

    # Output options
    output_options = parser.add_mutually_exclusive_group()
    output_options.add_argument('-q', '--quiet', dest='quiet', action='store_true',
            help="Disables any additional output except for errors.")
    output_options.add_argument('-v', '--verbose', dest='verbose', action='store_true',
            help="Enables verbose output.")

    # Check
    parser_check = subparsers.add_parser('check', help="Reports differences between the config that will be generated by autokernel, and the given config file. If no config file is given, the script will try to load the current kernel's configuration from '/proc/config.gz'.")
    parser_check.add_argument('-c', '--compare-config', nargs='?', dest='compare_config', type=check_file_exists,
            help="The .config file to compare the generated configuration against.")
    parser_check.set_defaults(func=main_check_config)

    # Config generation options
    parser_generate_config = subparsers.add_parser('generate-config', help='Generates the kernel configuration file from the autokernel configuration.')
    parser_generate_config.add_argument('-o', '--output', dest='output',
            help="The output filename. An existing configuration file will be overwritten. The default is '{KERNEL_DIR}/.config'.")
    parser_generate_config.set_defaults(func=main_generate_config)

    # Build options
    parser_build = subparsers.add_parser('build', help='Generates the configuration, and then builds the kernel (and initramfs if required) in the kernel tree.')
    parser_build.set_defaults(func=main_build)

    # Installation options
    parser_install = subparsers.add_parser('install', help='Installs the finished kernel and requisites into the system.')
    parser_install.set_defaults(func=main_install)

    # Full build options
    parser_all = subparsers.add_parser('all', help='First builds and then installs the kernel.')
    parser_all.set_defaults(func=main_build_all)

    # TODO
    parser_search = subparsers.add_parser('search', help='Searches for the given symbol and outputs a short summary')
    parser_search.add_argument('config_symbols', nargs='+',
            help="A list of configuration symbols to search for")
    parser_search.set_defaults(func=main_search)

    #TODO autokernel search CONFIG_SYSVIPC
    #TODO -l --limit [50]
    #TODO autokernel deps [CONFIG_]SYSVIPC
    # -c, --changes-only (only display dependencies deviating from the current configuration)
    #
    #TODO autokernel createmodule CONFIG_OPTIMIZE_INLINING=y CONFIG_A=y

    # Single config module generation options
    parser_deps = subparsers.add_parser('deps', help='Generates required modules to enable the given symbol')
    parser_deps.add_argument('-g', '--global', action='store_true', dest='dep_global',
            help="Report changes based on an allnoconfig instead of basing the output on changes from the current autokernel configuration")
    parser_deps.add_argument('-t', '--type', choices=['module', 'kconf'], dest='output_type',
            help="Selects the output type. 'kconf' will output options in the kernel configuration format. 'module' will output a list of autokernel modules to reflect the necessary configuration.")
    parser_deps.add_argument('-o', '--output', dest='output',
            help="Writes the output to the given file. Use - for stdout (default).")
    parser_deps.add_argument('config_symbol',
            help="The configuration symbol to generate dependencies for")
    parser_deps.set_defaults(func=main_deps)

    # Config detection options
    parser_detect = subparsers.add_parser('detect', help='TODO')
    parser_detect.add_argument('-c', '--check', nargs='?', default=0, dest='check_config', type=check_file_exists,
            help="Instead of outputting the required configuration values, compare the detected options against the given kernel configuration and report the status of each option. If no config file is given, the script will try to load the current kernel's configuration from '/proc/config.gz'.")
    parser_detect.add_argument('-t', '--type', choices=['module', 'kconf'], dest='output_type',
            help="Selects the output type. 'kconf' will output options in the kernel configuration format. 'module' will output a list of autokernel modules to reflect the necessary configuration.")
    parser_detect.add_argument('-m', '--module-name', dest='output_module_name', default='local',
            help="The name of the generated module, which will enable all detected options (default: 'local').")
    parser_detect.add_argument('-o', '--output', dest='output',
            help="Writes the output to the given file. Use - for stdout (default).")
    parser_detect.set_defaults(func=main_detect)

    # TODO static paths as global variable

    try:
        args = parser.parse_args()
    except ArgumentParserError as e:
        die(str(e))

    # Enable verbose logging if desired
    log.verbose_output = args.verbose
    log.quiet_output = args.quiet

    # Fallback to main_build_all() if no mode is given
    if 'func' not in args:
        main_build_all(args)
    else:
        # Execute the mode's function
        args.func(args)

    # TODO umask (probably better as external advice, use umask then execute this.)

if __name__ == '__main__':
    try:
        main()
    except PermissionError as e:
        die(str(e))
    except Exception as e:
        import traceback
        traceback.print_exc()
        die("Aborted because of previous errors")
