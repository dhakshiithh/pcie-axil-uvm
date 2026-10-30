"""
UVM-style (pyuvm) verification environment for the open-source
`pcie_axil_master` block from alexforencich/verilog-pcie.

DUT: receives PCIe request TLPs (MRd / MWr / IO / Msg / Cpl ...) on a TLP
interface, turns memory requests into AXI4-Lite transactions, and returns
completion TLPs.

Environment structure (classic UVM):

    PcieReqItem   - sequence item: one PCIe request (kind, addr, length, data)
    *Seq          - sequences that generate constrained-random / error traffic
    PcieDriver    - drives items onto the DUT through a PCIe root-complex model
                    (memory/IO requests) or by injecting raw TLPs (messages,
                    unexpected completions)
    CplMonitor    - passively watches the DUT's completion TLP output
    PcieScoreboard- reference memory model + PCIe protocol rule checks
    PcieCoverage  - functional coverage (request kind, length, alignment,
                    4KB boundary, back-pressure) with a bin-level report
    PcieEnv       - wires everything together
"""

import random

import cocotb
from cocotb.triggers import RisingEdge, ReadOnly

import pyuvm
from pyuvm import (uvm_sequence_item, uvm_sequence, uvm_sequencer, uvm_driver,
                   uvm_component, uvm_env, uvm_subscriber,
                   uvm_analysis_port, ConfigDB)

from pcie_if import PcieIfFrame

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Window of AXI address space used by the tests (inside BAR0)
MEM_BASE = 0x0000
MEM_SIZE = 0x4000  # 16 KB -> spans several 4 KB pages

CPL_STATUS = {0: "SC", 1: "UR", 2: "CRS", 4: "CA"}

KINDS = ["MEM_WR", "MEM_RD", "IO_WR", "IO_RD", "MSG", "MSG_DATA", "UNEXP_CPL"]
POSTED_KINDS = {"MEM_WR", "MSG", "MSG_DATA", "UNEXP_CPL"}


def length_bin(n):
    """Coverage bin for request length in bytes."""
    if n == 0:
        return "0"
    if n <= 3:
        return "1-3"
    if n == 4:
        return "4"
    if n <= 8:
        return "5-8"
    if n <= 32:
        return "9-32"
    if n <= 128:
        return "33-128"
    return ">128"


# ---------------------------------------------------------------------------
# Sequence item
# ---------------------------------------------------------------------------

class PcieReqItem(uvm_sequence_item):
    """One PCIe request plus the results observed when it was driven."""

    def __init__(self, name="pcie_req", kind="MEM_RD", addr=0, length=4, data=b""):
        super().__init__(name)
        self.kind = kind
        self.addr = addr
        self.length = length
        self.data = bytearray(data)
        # filled in by the driver
        self.rd_data = None
        self.exception = None
        self.cpls = []           # completions observed while this request was active
        self.violations = []     # protocol-checker violations seen while active
        self.err_cor = False
        self.err_uncor = False
        self.backpressure = False

    def crosses_4k(self):
        return self.length > 0 and (self.addr // 4096) != ((self.addr + self.length - 1) // 4096)

    def __str__(self):
        return f"{self.kind} addr=0x{self.addr:x} len={self.length}"


# ---------------------------------------------------------------------------
# Sequences
# ---------------------------------------------------------------------------

def _random_mem_addr_len(max_len):
    """Constrained-random address/length, biased toward corner cases."""
    choices = [0, 1, 2, 3, 4, 5, 7, 8,         # sub-DW and small
               random.randint(9, 32),          # a few DWs
               random.randint(33, min(128, max_len))]  # multi-beat
    if max_len > 128:
        choices.append(random.randint(129, max_len))  # long bursts
    length = random.choice(choices)
    style = random.random()
    if style < 0.3:
        # land near a 4 KB page boundary (PCIe requests must not cross it,
        # the root-complex model splits them; DUT must handle the pieces)
        page = random.randrange(0, MEM_SIZE // 4096) * 4096
        addr = page + 4096 - random.randint(1, 8)
    elif style < 0.6:
        addr = random.randrange(MEM_BASE, MEM_BASE + MEM_SIZE - max_len)
        addr = (addr & ~0x3) + random.randint(1, 3)   # deliberately unaligned
    else:
        addr = random.randrange(MEM_BASE, MEM_BASE + MEM_SIZE - max_len) & ~0x3
    addr = min(addr, MEM_BASE + MEM_SIZE - max(length, 1))
    return addr, length


class RandomMemSeq(uvm_sequence):
    """Constrained-random mix of memory writes and reads."""

    def __init__(self, name="random_mem_seq", n=200, max_len=256):
        super().__init__(name)
        self.n = n
        self.max_len = max_len

    async def body(self):
        for i in range(self.n):
            kind = random.choice(["MEM_WR", "MEM_RD", "MEM_WR"])  # write-biased so reads see fresh data
            addr, length = _random_mem_addr_len(self.max_len)
            data = bytes(random.getrandbits(8) for _ in range(length)) if kind == "MEM_WR" else b""
            item = PcieReqItem(f"mem_{i}", kind, addr, length, data)
            await self.start_item(item)
            await self.finish_item(item)


class ErrorInjectSeq(uvm_sequence):
    """Requests the DUT must reject or ignore: IO, messages, stray completions."""

    def __init__(self, name="error_inject_seq", rounds=4):
        super().__init__(name)
        self.rounds = rounds

    async def body(self):
        for r in range(self.rounds):
            for kind in ["IO_WR", "IO_RD", "MSG", "MSG_DATA", "UNEXP_CPL"]:
                addr = random.randrange(0, 0x100) & ~0x3
                data = bytes(random.getrandbits(8) for _ in range(4))
                item = PcieReqItem(f"err_{r}_{kind}", kind, addr, 4, data)
                await self.start_item(item)
                await self.finish_item(item)


class BoundaryReadSeq(uvm_sequence):
    """Directed sweep: reads of 1..40 DWs that start just before 64-byte
    (read completion boundary) and 4 KB boundaries, so completions get split."""

    async def body(self):
        i = 0
        for base in (0x0e00, 0x1000, 0x2040):
            for back in (4, 8, 12, 60):
                for dws in range(1, 41):
                    item = PcieReqItem(f"bnd_{i}", "MEM_RD", base - back, dws * 4)
                    await self.start_item(item)
                    await self.finish_item(item)
                    i += 1


class MixedSeq(uvm_sequence):
    """Random interleaving of legal traffic and error injection."""

    def __init__(self, name="mixed_seq", n=150, max_len=128):
        super().__init__(name)
        self.n = n
        self.max_len = max_len

    async def body(self):
        for i in range(self.n):
            if random.random() < 0.8:
                kind = random.choice(["MEM_WR", "MEM_RD"])
                addr, length = _random_mem_addr_len(self.max_len)
            else:
                kind = random.choice(["IO_WR", "IO_RD", "MSG", "MSG_DATA", "UNEXP_CPL"])
                addr, length = random.randrange(0, 0x100) & ~0x3, 4
            data = bytes(random.getrandbits(8) for _ in range(length)) if kind not in ("MEM_RD", "IO_RD") else b""
            item = PcieReqItem(f"mix_{i}", kind, addr, length, data)
            await self.start_item(item)
            await self.finish_item(item)


# ---------------------------------------------------------------------------
# Completion monitor (passive)
# ---------------------------------------------------------------------------

class CplObs:
    def __init__(self, status, tag, byte_count, length_dw, requester_id):
        self.status = status
        self.tag = tag
        self.byte_count = byte_count
        self.length_dw = length_dw
        self.requester_id = requester_id

    def __repr__(self):
        return (f"Cpl(status={CPL_STATUS.get(self.status, self.status)}, tag={self.tag}, "
                f"bc={self.byte_count}, len_dw={self.length_dw})")


class CplMonitor(uvm_component):
    """Samples tx_cpl_tlp_* on every clock; records each completion header."""

    def build_phase(self):
        self.ap = uvm_analysis_port("ap", self)
        self.dut = cocotb.top
        self.observed = []     # driver reads this list to attribute completions
        self.violations = []   # protocol-checker failures (strobe / framing)
        self.lanes = len(self.dut.tx_cpl_tlp_strb)   # DWs per beat

    async def run_phase(self):
        dut = self.dut
        remaining = 0          # data DWs still expected in the current TLP
        cur = None
        beat = 0
        while True:
            await RisingEdge(dut.clk)
            await ReadOnly()
            if not (int(dut.tx_cpl_tlp_valid.value) and int(dut.tx_cpl_tlp_ready.value)):
                continue
            strb = int(dut.tx_cpl_tlp_strb.value)
            if int(dut.tx_cpl_tlp_sop.value):
                hdr = int(dut.tx_cpl_tlp_hdr.value)
                cur = CplObs(
                    status=(hdr >> 77) & 0x7,
                    tag=((hdr >> 40) & 0xff) | (((hdr >> 115) & 1) << 8) | (((hdr >> 119) & 1) << 9),
                    byte_count=(hdr >> 64) & 0xfff,
                    length_dw=(hdr >> 96) & 0x3ff,
                    requester_id=(hdr >> 48) & 0xffff,
                )
                cur.lower_addr = (hdr >> 32) & 0x7f
                has_data = bool(hdr & (1 << 126))
                remaining = (cur.length_dw or 1024) if has_data else 0
                beat = 0
                self.observed.append(cur)
                self.ap.write(cur)

            # Protocol check: strobe must mark exactly the valid data DWs of this beat
            n = min(remaining, self.lanes)
            exp_strb = (1 << n) - 1
            if strb != exp_strb:
                self.violations.append(
                    f"strobe violation: {cur} lower_addr=0x{cur.lower_addr:02x} beat={beat} "
                    f"strb=0b{strb:0{self.lanes}b} expected=0b{exp_strb:0{self.lanes}b}")
                self.logger.error(self.violations[-1])
            remaining -= n
            beat += 1
            if int(dut.tx_cpl_tlp_eop.value):
                if remaining != 0:
                    self.violations.append(f"framing violation: eop with {remaining} DW outstanding in {cur}")
                    self.logger.error(self.violations[-1])
                cur = None


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

class PcieDriver(uvm_driver):
    """Drives each request through the root-complex model or as a raw TLP."""

    def build_phase(self):
        self.ap = uvm_analysis_port("ap", self)

    def connect_phase(self):
        self.h = ConfigDB().get(self, "", "HARNESS")
        self.mon = ConfigDB().get(self, "", "CPL_MON")

    async def run_phase(self):
        while True:
            item = await self.seq_item_port.get_next_item()
            await self.drive(item)
            self.ap.write(item)
            self.seq_item_port.item_done()

    async def drive(self, item):
        h = self.h
        h.clear_error_flags()
        start = len(self.mon.observed)
        vstart = len(self.mon.violations)
        item.backpressure = h.backpressure_enabled

        try:
            if item.kind == "MEM_WR":
                await h.bar0.write(item.addr, item.data)
            elif item.kind == "MEM_RD":
                item.rd_data = await h.bar0.read(item.addr, item.length,
                                                 timeout=20000, timeout_unit="ns")
            elif item.kind == "IO_WR":
                await h.bar1.write(item.addr, item.data, timeout=20000, timeout_unit="ns")
            elif item.kind == "IO_RD":
                item.rd_data = await h.bar1.read(item.addr, 4, timeout=20000, timeout_unit="ns")
            elif item.kind in ("MSG", "MSG_DATA"):
                await h.inject_frame(h.build_vendor_msg(with_data=(item.kind == "MSG_DATA"),
                                                        data=item.data))
            elif item.kind == "UNEXP_CPL":
                await h.inject_frame(h.build_unexpected_cpl())
        except Exception as e:  # completion with UR/CA status or timeout
            item.exception = str(e)

        await h.wait_quiet()

        item.cpls = self.mon.observed[start:]
        item.violations = self.mon.violations[vstart:]
        item.err_cor = h.err_cor
        item.err_uncor = h.err_uncor


# ---------------------------------------------------------------------------
# Scoreboard
# ---------------------------------------------------------------------------

class PcieScoreboard(uvm_subscriber):
    """Reference model + protocol checks.

    * Memory model: every MEM_WR updates a byte-accurate model; every MEM_RD and
      the AXI RAM contents (checked via back-door after each write, including
      guard bytes around the write) must match the model.
    * Protocol rules:
        - posted requests (MWr, Msg, MsgD) must NEVER produce a completion
        - an unexpected completion must be dropped (no completion back) and flag
          a correctable error
        - IO requests are unsupported: UR completion + correctable error, and
          they must not touch AXI memory
        - legal memory traffic must never raise error status
    """

    def build_phase(self):
        self.model = bytearray(MEM_SIZE)
        self.errors = []
        self.checked = 0

    def connect_phase(self):
        self.h = ConfigDB().get(self, "", "HARNESS")

    def start_of_simulation_phase(self):
        # model starts as a copy of the initial RAM image
        self.model[:] = self.h.axil_ram.read(MEM_BASE, MEM_SIZE)

    def _fail(self, item, msg):
        full = f"[{item.get_name()}] {item}: {msg}"
        self.errors.append(full)
        self.logger.error(full)

    def write(self, item):
        self.checked += 1
        k = item.kind
        h = self.h

        if k in POSTED_KINDS and item.cpls:
            self._fail(item, f"posted request produced completion(s) {item.cpls} "
                             f"(PCIe: posted requests must not be completed)")

        if k == "MEM_WR":
            if item.exception:
                self._fail(item, f"unexpected exception {item.exception}")
            off = item.addr - MEM_BASE
            self.model[off:off + item.length] = item.data
            lo = max(0, off - 4)
            hi = min(MEM_SIZE, off + item.length + 4)   # include guard bytes
            actual = h.axil_ram.read(MEM_BASE + lo, hi - lo)
            if actual != self.model[lo:hi]:
                self._fail(item, "AXI memory does not match reference model after write")
            if item.err_cor or item.err_uncor:
                self._fail(item, "error status asserted for a legal write")

        elif k == "MEM_RD":
            off = item.addr - MEM_BASE
            exp = bytes(self.model[off:off + item.length])
            if item.exception:
                self._fail(item, f"read failed: {item.exception}")
            elif bytes(item.rd_data) != exp:
                self._fail(item, f"read data mismatch: got {bytes(item.rd_data).hex()} "
                                 f"exp {exp.hex()}")
            if any(c.status != 0 for c in item.cpls):
                self._fail(item, f"non-SC completion for legal read: {item.cpls}")
            if item.err_cor or item.err_uncor:
                self._fail(item, "error status asserted for a legal read")

        elif k in ("IO_WR", "IO_RD"):
            if not item.exception or "Unsuccessful completion" not in item.exception:
                self._fail(item, f"IO request was not rejected with UR (exception={item.exception})")
            if not any(c.status == 1 for c in item.cpls):
                self._fail(item, f"expected a UR completion, saw {item.cpls}")
            if not item.err_cor:
                self._fail(item, "IO request did not flag a correctable error")
            if h.axil_ram.read(MEM_BASE, MEM_SIZE) != bytes(self.model):
                self._fail(item, "IO request modified AXI memory")

        elif k == "UNEXP_CPL":
            if not item.err_cor:
                self._fail(item, "unexpected completion did not flag a correctable error")

        elif k in ("MSG", "MSG_DATA"):
            # Vendor-Defined Type 1 messages are posted. Per the DUT's own design
            # intent (see RTL comment "message - posted, no completion") it must
            # not send a completion. Completion presence is checked above.
            pass

    def report_phase(self):
        self.logger.info(f"Scoreboard: {self.checked} transactions checked, "
                         f"{len(self.errors)} error(s)")


# ---------------------------------------------------------------------------
# Functional coverage
# ---------------------------------------------------------------------------

class PcieCoverage(uvm_subscriber):
    """Hand-rolled covergroup with bins + crosses and a percentage report."""

    LEN_BINS = ["0", "1-3", "4", "5-8", "9-32", "33-128", ">128"]

    def build_phase(self):
        self.bins = {}
        self.define("kind", KINDS)
        self.define("len", self.LEN_BINS)
        self.define("align", ["0", "1", "2", "3"])
        self.define("cross_4k_page", ["no", "yes"])
        self.define("backpressure", ["off", "on"])
        self.define("kind_x_len", [f"{k}:{l}" for k in ("MEM_WR", "MEM_RD") for l in self.LEN_BINS])
        self.define("kind_x_bp", [f"{k}:{b}" for k in ("MEM_WR", "MEM_RD") for b in ("off", "on")])
        # reads > 32 DW are split into completions on 128-byte RCB boundaries;
        # 'first_1dw' = first chunk is a single DW (the corner case that exposed bug #2)
        self.define("rd_rcb_split", ["single_cpl", "multi_cpl", "first_1dw"])

    def define(self, name, bins):
        self.bins[name] = {b: 0 for b in bins}

    def hit(self, name, b):
        if b in self.bins[name]:
            self.bins[name][b] += 1

    def write(self, item):
        bp = "on" if item.backpressure else "off"
        self.hit("kind", item.kind)
        self.hit("backpressure", bp)
        if item.kind in ("MEM_WR", "MEM_RD"):
            lb = length_bin(item.length)
            self.hit("len", lb)
            self.hit("align", str(item.addr % 4))
            self.hit("cross_4k_page", "yes" if item.crosses_4k() else "no")
            self.hit("kind_x_len", f"{item.kind}:{lb}")
            self.hit("kind_x_bp", f"{item.kind}:{bp}")
        if item.kind == "MEM_RD" and item.length > 0:
            first_dw = item.addr & ~0x3
            n_dw = ((item.addr + item.length + 3) & ~0x3) - first_dw
            n_dw //= 4
            if n_dw <= 32:
                self.hit("rd_rcb_split", "single_cpl")
            elif (first_dw >> 2) & 0x1f == 31:
                self.hit("rd_rcb_split", "first_1dw")
            else:
                self.hit("rd_rcb_split", "multi_cpl")

    def summary(self):
        lines = []
        tot_hit = tot = 0
        for name, bins in self.bins.items():
            hit = sum(1 for v in bins.values() if v)
            tot_hit += hit
            tot += len(bins)
            lines.append(f"  {name:<15} {hit:>3}/{len(bins):<3} "
                         f"({100.0 * hit / len(bins):5.1f}%)  " +
                         ", ".join(f"{b}={v}" for b, v in bins.items()))
        pct = 100.0 * tot_hit / tot
        return pct, "Functional coverage: %.1f%% (%d/%d bins)\n" % (pct, tot_hit, tot) + "\n".join(lines)

    def report_phase(self):
        pct, text = self.summary()
        self.logger.info("\n" + text)


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

class PcieEnv(uvm_env):
    def build_phase(self):
        self.seqr = uvm_sequencer("seqr", self)
        ConfigDB().set(None, "*", "SEQR", self.seqr)
        self.mon = CplMonitor("cpl_mon", self)
        ConfigDB().set(None, "*", "CPL_MON", self.mon)
        self.drv = PcieDriver("drv", self)
        self.sb = PcieScoreboard("sb", self)
        self.cov = PcieCoverage("cov", self)

    def connect_phase(self):
        self.drv.seq_item_port.connect(self.seqr.seq_item_export)
        self.drv.ap.connect(self.sb.analysis_export)
        self.drv.ap.connect(self.cov.analysis_export)
        ConfigDB().set(None, "*", "ENV", self)
