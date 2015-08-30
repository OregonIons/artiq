import os, sys, tempfile, subprocess
from llvmlite_artiq import ir as ll, binding as llvm

llvm.initialize()
llvm.initialize_all_targets()
llvm.initialize_all_asmprinters()

class RunTool:
    def __init__(self, pattern, **tempdata):
        self.files = []
        self.pattern = pattern
        self.tempdata = tempdata

    def maketemp(self, data):
        f = tempfile.NamedTemporaryFile()
        f.write(data)
        f.flush()
        self.files.append(f)
        return f

    def __enter__(self):
        tempfiles = {}
        tempnames = {}
        for key in self.tempdata:
            tempfiles[key] = self.maketemp(self.tempdata[key])
            tempnames[key] = tempfiles[key].name

        cmdline = []
        for argument in self.pattern:
            cmdline.append(argument.format(**tempnames))

        process = subprocess.Popen(cmdline, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = process.communicate()
        if process.returncode != 0:
            raise Exception("{} invocation failed: {}".
                            format(cmdline[0], stderr.decode('utf-8')))

        tempfiles["__stdout__"] = stdout.decode('utf-8')
        return tempfiles

    def __exit__(self, exc_typ, exc_value, exc_trace):
        for f in self.files:
            f.close()

class Target:
    """
    A description of the target environment where the binaries
    generated by the ARTIQ compiler will be deployed.

    :var triple: (string)
        LLVM target triple, e.g. ``"or1k"``
    :var data_layout: (string)
        LLVM target data layout, e.g. ``"E-m:e-p:32:32-i64:32-f64:32-v64:32-v128:32-a:0:32-n32"``
    :var features: (list of string)
        LLVM target CPU features, e.g. ``["mul", "div", "ffl1"]``
    :var print_function: (string)
        Name of a formatted print functions (with the signature of ``printf``)
        provided by the target, e.g. ``"printf"``.
    """
    triple = "unknown"
    data_layout = ""
    features = []
    print_function = "printf"


    def __init__(self):
        self.llcontext = ll.Context()

    def compile(self, module):
        """Compile the module to a relocatable object for this target."""

        if os.getenv("ARTIQ_DUMP_SIG"):
            print("====== MODULE_SIGNATURE DUMP ======", file=sys.stderr)
            print(module, file=sys.stderr)

        if os.getenv("ARTIQ_DUMP_IR"):
            print("====== ARTIQ IR DUMP ======", file=sys.stderr)
            for function in module.artiq_ir:
                print(function, file=sys.stderr)

        llmod = module.build_llvm_ir(self)
        llparsedmod = llvm.parse_assembly(str(llmod))
        llparsedmod.verify()

        if os.getenv("ARTIQ_DUMP_LLVM"):
            print("====== LLVM IR DUMP ======", file=sys.stderr)
            print(str(llparsedmod), file=sys.stderr)

        llpassmgrbuilder = llvm.create_pass_manager_builder()
        llpassmgrbuilder.opt_level  = 2 # -O2
        llpassmgrbuilder.size_level = 1 # -Os

        llpassmgr = llvm.create_module_pass_manager()
        llpassmgrbuilder.populate(llpassmgr)
        llpassmgr.run(llparsedmod)

        if os.getenv("ARTIQ_DUMP_LLVM"):
            print("====== LLVM IR DUMP (OPTIMIZED) ======", file=sys.stderr)
            print(str(llparsedmod), file=sys.stderr)

        lltarget = llvm.Target.from_triple(self.triple)
        llmachine = lltarget.create_target_machine(
                        features=",".join(["+{}".format(f) for f in self.features]),
                        reloc="pic", codemodel="default")

        if os.getenv("ARTIQ_DUMP_ASSEMBLY"):
            print("====== ASSEMBLY DUMP ======", file=sys.stderr)
            print(llmachine.emit_assembly(llparsedmod), file=sys.stderr)

        return llmachine.emit_object(llparsedmod)

    def link(self, objects, init_fn):
        """Link the relocatable objects into a shared library for this target."""
        with RunTool([self.triple + "-ld", "-shared", "--eh-frame-hdr", "-init", init_fn] +
                     ["{{obj{}}}".format(index) for index in range(len(objects))] +
                     ["-o", "{output}"],
                     output=b"",
                     **{"obj{}".format(index): obj for index, obj in enumerate(objects)}) \
                as results:
            library = results["output"].read()

            if os.getenv("ARTIQ_DUMP_ELF"):
                shlib_temp = tempfile.NamedTemporaryFile(suffix=".so", delete=False)
                shlib_temp.write(library)
                shlib_temp.close()
                print("====== SHARED LIBRARY DUMP ======", file=sys.stderr)
                print("Shared library dumped as {}".format(shlib_temp.name), file=sys.stderr)

            return library

    def compile_and_link(self, modules):
        return self.link([self.compile(module) for module in modules],
                         init_fn=modules[0].entry_point())

    def strip(self, library):
        with RunTool([self.triple + "-strip", "--strip-debug", "{library}", "-o", "{output}"],
                     library=library, output=b"") \
                as results:
            return results["output"].read()

    def symbolize(self, library, addresses):
        # Addresses point one instruction past the jump; offset them back by 1.
        offset_addresses = [hex(addr - 1) for addr in addresses]
        with RunTool([self.triple + "-addr2line", "--functions", "--inlines",
                      "--exe={library}"] + offset_addresses,
                     library=library) \
                as results:
            lines = results["__stdout__"].rstrip().split("\n")
            backtrace = []
            for function_name, location, address in zip(lines[::2], lines[1::2], addresses):
                filename, line = location.rsplit(":", 1)
                if filename == "??":
                    continue
                # can't get column out of addr2line D:
                backtrace.append((filename, int(line), -1, function_name, address))
            return backtrace

class NativeTarget(Target):
    def __init__(self):
        super().__init__()
        self.triple = llvm.get_default_triple()

class OR1KTarget(Target):
    triple = "or1k-linux"
    data_layout = "E-m:e-p:32:32-i64:32-f64:32-v64:32-v128:32-a:0:32-n32"
    features = ["mul", "div", "ffl1", "cmov", "addc"]
    print_function = "lognonl"
