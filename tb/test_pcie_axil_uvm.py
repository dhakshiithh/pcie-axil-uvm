"""
pyuvm tests for pcie_axil_master.

    MemRandomTest   - constrained-random memory reads/writes, with and without
                      random back-pressure on every handshake
    ErrorInjectTest - IO requests, vendor-defined messages and unexpected
                      completions (requests the DUT must reject or ignore)
    MixedTest       - random interleaving of legal traffic and error injection
                      under back-pressure
"""

import os
import json
import random
import struct
import logging

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ReadOnly

import pyuvm
from pyuvm import uvm_test, ConfigDB

from cocotbext.pcie.core import RootComplex
from cocotbext.pcie.core.tlp import Tlp, TlpType
from cocotbext.axi import AxiLiteBus, AxiLiteRam

from pcie_if import PcieIfDevice, PcieIfRxBus, PcieIfTxBus, PcieIfFrame
from pcie_uvm_env import (PcieEnv, RandomMemSeq, ErrorInjectSeq, MixedSeq, BoundaryReadSeq,
                          MEM_BASE, MEM_SIZE)

REPORT_DIR = os.environ.get("REPORT_DIR", os.path.join(os.path.dirname(__file__), "..", "reports"))


# ---------------------------------------------------------------------------
# Harness: clock/reset, PCIe root complex + device model, AXI-Lite RAM
# ---------------------------------------------------------------------------

class Harness:
    def __init__(self, dut):
        self.dut = dut
        self.log = logging.getLogger("cocotb.harness")
        cocotb.start_soon(Clock(dut.clk, 4, units="ns").start())

        self.rc = RootComplex()
        self.dev = PcieIfDevice(
            clk=dut.clk, rst=dut.rst,
            rx_req_tlp_bus=PcieIfRxBus.from_prefix(dut, "rx_req_tlp"),
            tx_cpl_tlp_bus=PcieIfTxBus.from_prefix(dut, "tx_cpl_tlp"),
        )
        self.dev.functions[0].configure_bar(0, 16 * 1024 * 1024)        # BAR0: memory
        self.dev.functions[0].configure_bar(1, 16 * 1024, io=True)      # BAR1: IO (unsupported by DUT)
        self.rc.make_port().connect(self.dev)
        # our CplMonitor implements the strobe/framing protocol check and logs
        # violations instead of aborting the simulation, so disable the model's
        # built-in hard assertion
        self.dev.tx_cpl_tlp_sink.strb_present = False

        self.axil_ram = AxiLiteRam(AxiLiteBus.from_prefix(dut, "m_axil"), dut.clk, dut.rst, size=2 ** 16)
        # non-zero background pattern so lost/misplaced writes are visible
        self.axil_ram.write(0, bytes((i * 7 + 3) & 0xff for i in range(2 ** 16)))

        dut.completer_id.setimmediatevalue(0)
        self.err_cor = False
        self.err_uncor = False
        self.backpressure_enabled = False
        cocotb.start_soon(self._watch_errors())

    async def _watch_errors(self):
        while True:
            await RisingEdge(self.dut.clk)
            await ReadOnly()
            if int(self.dut.status_error_cor.value):
                self.err_cor = True
            if int(self.dut.status_error_uncor.value):
                self.err_uncor = True

    def clear_error_flags(self):
        self.err_cor = False
        self.err_uncor = False

    def enable_backpressure(self, p=0.4):
        """Randomly stall every valid/ready handshake on both sides of the DUT."""
        def gen():
            while True:
                yield 1 if random.random() < p else 0
        self.dev.rx_req_tlp_source.set_pause_generator(gen())
        self.dev.tx_cpl_tlp_sink.set_pause_generator(gen())
        for ch in (self.axil_ram.write_if.aw_channel, self.axil_ram.write_if.w_channel,
                   self.axil_ram.write_if.b_channel, self.axil_ram.read_if.ar_channel,
                   self.axil_ram.read_if.r_channel):
            ch.set_pause_generator(gen())
        self.backpressure_enabled = True

    async def start(self):
        dut = self.dut
        dut.rst.setimmediatevalue(0)
        for v in (0, 1, 1, 0, 0):
            await RisingEdge(dut.clk)
            dut.rst.value = v
        await self.rc.enumerate()
        self.devh = self.rc.find_device(self.dev.functions[0].pcie_id)
        await self.devh.enable_device()
        self.bar0 = self.devh.bar_window[0]
        self.bar1 = self.devh.bar_window[1]
        dut.completer_id.value = int(self.dev.functions[0].pcie_id)

    async def inject_frame(self, frame):
        await self.dev.rx_req_tlp_source.send(frame)

    def build_vendor_msg(self, with_data=False, data=b"\0\0\0\0"):
        """Vendor_Defined Type 1 message, routed by ID (4DW header).

        fmt = 001 (Msg) / 011 (MsgD), type = 10010 (route by ID),
        message code 0x7F. Receivers must silently discard unsupported Type 1
        vendor messages; in any case a posted message must never be completed.
        """
        fmt = 0b011 if with_data else 0b001
        typ = 0b10010
        length = 1 if with_data else 0
        req_id = 0x0000                      # root complex
        dest_id = int(self.dev.functions[0].pcie_id)
        dw0 = (fmt << 29) | (typ << 24) | length
        dw1 = (req_id << 16) | (0 << 8) | 0x7F
        dw2 = (dest_id << 16) | 0x1234       # vendor ID
        dw3 = 0
        frame = PcieIfFrame()
        frame.hdr = (dw0 << 96) | (dw1 << 64) | (dw2 << 32) | dw3
        if with_data:
            frame.data = list(struct.unpack("<L", bytes(data[:4]).ljust(4, b"\0")))
        frame.update_parity()
        return frame

    def build_unexpected_cpl(self):
        """A completion the DUT never asked for (it is a completer only)."""
        tlp = Tlp()
        tlp.fmt_type = TlpType.CPL
        tlp.requester_id = self.dev.functions[0].pcie_id
        tlp.completer_id = 0
        tlp.tag = random.randrange(256)
        tlp.byte_count = 4
        tlp.status = 0
        return PcieIfFrame.from_tlp(tlp)

    async def wait_quiet(self, cycles=40):
        """Wait until no TLP or AXI handshake activity for `cycles` clocks."""
        dut = self.dut
        quiet = 0
        sigs = (dut.rx_req_tlp_valid, dut.tx_cpl_tlp_valid, dut.m_axil_awvalid,
                dut.m_axil_wvalid, dut.m_axil_bvalid, dut.m_axil_arvalid, dut.m_axil_rvalid)
        while quiet < cycles:
            await RisingEdge(dut.clk)
            await ReadOnly()
            busy = any(int(s.value) for s in sigs) or not self.dev.rx_req_tlp_source.empty()
            quiet = 0 if busy else quiet + 1
        await RisingEdge(dut.clk)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class BaseTest(uvm_test):
    backpressure_first = False

    def build_phase(self):
        seed = int(os.environ.get("RANDOM_SEED", cocotb.RANDOM_SEED))
        random.seed(seed)
        self.seed = seed
        self.h = Harness(cocotb.top)
        ConfigDB().set(None, "*", "HARNESS", self.h)
        self.env = PcieEnv("env", self)

    async def run_phase(self):
        self.raise_objection()
        await self.h.start()
        seqr = ConfigDB().get(self, "", "SEQR")
        await self.body(seqr)
        await self.h.wait_quiet()
        self.drop_objection()

    async def body(self, seqr):
        raise NotImplementedError

    def report_phase(self):
        pct, text = self.env.cov.summary()
        os.makedirs(REPORT_DIR, exist_ok=True)
        name = type(self).__name__
        with open(os.path.join(REPORT_DIR, f"{name}.txt"), "w") as f:
            f.write(f"Test: {name}  seed: {self.seed}\n")
            f.write(f"Transactions checked: {self.env.sb.checked}\n")
            f.write(f"Scoreboard errors: {len(self.env.sb.errors)}\n")
            for e in self.env.sb.errors[:50]:
                f.write(f"  - {e}\n")
            f.write(f"Protocol-checker violations: {len(self.env.mon.violations)}\n")
            for e in self.env.mon.violations[:50]:
                f.write(f"  - {e}\n")
            f.write("\n" + text + "\n")
        with open(os.path.join(REPORT_DIR, f"{name}.cov.json"), "w") as f:
            json.dump(self.env.cov.bins, f)

    def check_phase(self):
        problems = self.env.sb.errors + self.env.mon.violations
        assert not problems, f"{len(problems)} error(s); first: {problems[0]}"


class MemRandomTest(BaseTest):
    async def body(self, seqr):
        await RandomMemSeq("no_bp", n=int(os.environ.get("N_TXN", 250))).start(seqr)
        self.h.enable_backpressure()
        await RandomMemSeq("with_bp", n=int(os.environ.get("N_TXN", 250))).start(seqr)


class ErrorInjectTest(BaseTest):
    async def body(self, seqr):
        await ErrorInjectSeq(rounds=3).start(seqr)
        self.h.enable_backpressure()
        await ErrorInjectSeq(rounds=3).start(seqr)


class BoundarySweepTest(BaseTest):
    async def body(self, seqr):
        self.bad = []
        drv = self.env.drv
        orig = drv.drive

        async def drive_and_log(item):
            await orig(item)
            if item.violations:
                self.bad.append((item.addr, item.length // 4, len(item.cpls),
                                 [c.length_dw for c in item.cpls]))
        drv.drive = drive_and_log
        await BoundaryReadSeq().start(seqr)

    def report_phase(self):
        super().report_phase()
        name = type(self).__name__
        with open(os.path.join(REPORT_DIR, f"{name}_failing_requests.txt"), "w") as f:
            f.write("addr  len_dw  n_cpls  cpl_len_dw\n")
            for a, l, n, ls in self.bad:
                f.write(f"0x{a:04x} {l:3d} {n:3d} {ls}\n")


class MixedTest(BaseTest):
    async def body(self, seqr):
        self.h.enable_backpressure()
        await MixedSeq(n=int(os.environ.get("N_TXN", 200))).start(seqr)


# ---------------------------------------------------------------------------
# cocotb entry points (one per UVM test)
# ---------------------------------------------------------------------------

@cocotb.test()
async def mem_random_test(dut):
    await pyuvm.uvm_root().run_test(MemRandomTest)


@cocotb.test()
async def error_inject_test(dut):
    await pyuvm.uvm_root().run_test(ErrorInjectTest)


@cocotb.test()
async def mixed_test(dut):
    await pyuvm.uvm_root().run_test(MixedTest)


@cocotb.test()
async def boundary_sweep_test(dut):
    await pyuvm.uvm_root().run_test(BoundarySweepTest)
