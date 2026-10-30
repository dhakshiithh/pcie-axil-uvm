# cocotb + pyuvm regression for pcie_axil_master
#   make                      # run all tests (Icarus)
#   make TESTCASE=MemRandomTest
#   make TLP_DATA_WIDTH=256   # wider TLP datapath
#   make WAVES=1              # dump FST waveforms

TOPLEVEL_LANG = verilog
SIM ?= icarus
WAVES ?= 0

COCOTB_HDL_TIMEUNIT = 1ns
COCOTB_HDL_TIMEPRECISION = 1ps
export COCOTB_RESOLVE_X ?= RANDOM

TOPLEVEL = pcie_axil_master
MODULE   = test_pcie_axil_uvm
RTL ?= pcie_axil_master
VERILOG_SOURCES = $(PWD)/../rtl/$(RTL).v

TLP_DATA_WIDTH ?= 64
export PARAM_TLP_DATA_WIDTH := $(TLP_DATA_WIDTH)
export PARAM_TLP_STRB_WIDTH := $(shell expr $(TLP_DATA_WIDTH) / 32)
export PARAM_TLP_HDR_WIDTH := 128
export PARAM_TLP_SEG_COUNT := 1
export PARAM_AXIL_DATA_WIDTH := 32
export PARAM_AXIL_ADDR_WIDTH := 64
export PARAM_AXIL_STRB_WIDTH := 4
export PARAM_TLP_FORCE_64_BIT_ADDR := 0

export REPORT_DIR := $(PWD)/../reports/$(RTL)_w$(TLP_DATA_WIDTH)

ifeq ($(SIM), icarus)
	COMPILE_ARGS += $(foreach v,$(filter PARAM_%,$(.VARIABLES)),-P $(TOPLEVEL).$(subst PARAM_,,$(v))=$($(v)))
	ifeq ($(WAVES), 1)
		VERILOG_SOURCES += iverilog_dump.v
		COMPILE_ARGS += -s iverilog_dump
	endif
endif

include $(shell cocotb-config --makefiles)/Makefile.sim

iverilog_dump.v:
	echo 'module iverilog_dump(); initial begin $$dumpfile("$(TOPLEVEL).fst"); $$dumpvars(0, $(TOPLEVEL)); end endmodule' > $@

clean::
	rm -rf iverilog_dump.v *.fst results.xml __pycache__ sim_build
