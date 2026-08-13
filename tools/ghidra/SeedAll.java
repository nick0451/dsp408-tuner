//Seeds from the vector table AND from Thumb function prologues, then reports coverage.
//@category Analysis
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.lang.Register;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.FunctionIterator;
import java.math.BigInteger;

public class SeedAll extends GhidraScript {
    private Register tmode;

    private boolean seed(Address t) {
        try {
            if (tmode != null) {
                currentProgram.getProgramContext().setValue(tmode, t, t, BigInteger.ONE);
            }
            disassemble(t);
            if (getFunctionAt(t) == null) createFunction(t, null);
            return true;
        } catch (Exception e) { return false; }
    }

    @Override
    public void run() throws Exception {
        Address base = currentProgram.getMinAddress();
        Address max  = currentProgram.getMaxAddress();
        tmode = currentProgram.getRegister("TMode");
        long lo = base.getOffset(), hi = max.getOffset();

        int vec = 0;
        for (int i = 1; i < 128; i++) {
            Address slot = base.add(i * 4L);
            long v;
            try { v = getInt(slot) & 0xFFFFFFFFL; } catch (Exception e) { continue; }
            if ((v & 1L) == 0) continue;
            long t = v & ~1L;
            if (t < lo || t > hi) continue;
            if (seed(base.getNewAddress(t))) vec++;
        }
        println("SEEDED_VECTORS " + vec);

        // Thumb prologues: PUSH {..., LR} == 0xB5xx ; PUSH.W {..., LR} == 0xE92D 0x4xxx
        int pro = 0;
        for (long a = lo + 0x200; a < hi - 4; a += 2) {
            Address addr = base.getNewAddress(a);
            int w0;
            try { w0 = getShort(addr) & 0xFFFF; } catch (Exception e) { continue; }
            boolean hit = ((w0 & 0xFF00) == 0xB500);
            if (!hit && w0 == 0xE92D) {
                int w1;
                try { w1 = getShort(base.getNewAddress(a + 2)) & 0xFFFF; } catch (Exception e) { continue; }
                hit = (w1 & 0xF000) == 0x4000;
            }
            if (!hit) continue;
            if (getFunctionContaining(addr) != null) continue;
            if (seed(addr)) pro++;
        }
        println("SEEDED_PROLOGUES " + pro);

        long covered = 0; int n = 0;
        FunctionIterator it = currentProgram.getFunctionManager().getFunctions(true);
        while (it.hasNext()) {
            Function f = it.next();
            covered += f.getBody().getNumAddresses();
            n++;
        }
        println("COVERAGE " + n + " functions, " + covered + " / " + (hi - lo + 1) + " bytes");
    }
}
