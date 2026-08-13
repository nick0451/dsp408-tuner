//Exports every decompiled function to a single text file.
//@category Analysis
import ghidra.app.script.GhidraScript;
import ghidra.app.decompiler.*;
import ghidra.program.model.listing.*;
import java.io.*;

public class ExportDecomp extends GhidraScript {
    @Override
    public void run() throws Exception {
        String out = getScriptArgs().length > 0 ? getScriptArgs()[0] : "decomp.c";
        DecompInterface di = new DecompInterface();
        di.openProgram(currentProgram);
        PrintWriter w = new PrintWriter(new BufferedWriter(new FileWriter(out)));
        int n = 0, ok = 0;
        FunctionIterator it = currentProgram.getFunctionManager().getFunctions(true);
        while (it.hasNext() && !monitor.isCancelled()) {
            Function f = it.next();
            n++;
            DecompileResults r = di.decompileFunction(f, 60, monitor);
            w.println("/* ==== " + f.getName() + " @ " + f.getEntryPoint() + " ==== */");
            if (r != null && r.decompileCompleted()) {
                w.println(r.getDecompiledFunction().getC());
                ok++;
            } else {
                w.println("// decompilation failed");
            }
            w.println();
        }
        w.close();
        println("EXPORTED " + ok + "/" + n + " functions to " + out);
    }
}
