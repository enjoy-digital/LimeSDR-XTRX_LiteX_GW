#!/usr/bin/env python3

#
# This file is part of LiteX-XTRX.
#
# Copyright (c) 2021-2024 Enjoy-Digital <enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

import os
import argparse

from migen import *

from litex.gen import *

import fairwaves_xtrx_platform
import limesdr_xtrx_platform

from litex.soc.interconnect.csr import *
from litex.soc.interconnect     import stream

from litex.soc.integration.soc      import *
from litex.soc.integration.soc_core import *
from litex.soc.integration.builder  import *

from litex.soc.cores.clock     import *
from litex.soc.cores.led       import LedChaser
from litex.soc.cores.icap      import ICAP
from litex.soc.cores.xadc      import XADC
from litex.soc.cores.dna       import DNA
from litex.soc.cores.gpio      import GPIOOut
from litex.soc.cores.spi_flash import S7SPIFlash
from litex.soc.cores.bitbang   import I2CMaster
from litex.soc.cores.spi       import SPIMaster

from litepcie.phy.s7pciephy import S7PCIEPHY

from litescope import LiteScopeAnalyzer

from gateware.aux import AUX

from software import generate_litepcie_software

# CRG ----------------------------------------------------------------------------------------------

class CRG(LiteXModule):
    def __init__(self, platform, sys_clk_freq):
        self.cd_sys    = ClockDomain()
        self.cd_idelay = ClockDomain()

        # # #

        # Clk / Rst.
        clk125 = ClockSignal("pcie")
        rst125 = ResetSignal("pcie")

        # PLL.
        self.pll = pll = S7PLL(speedgrade=-1)
        self.comb += pll.reset.eq(rst125)
        pll.register_clkin(clk125, 125e6)
        pll.create_clkout(self.cd_sys,    sys_clk_freq)
        pll.create_clkout(self.cd_idelay, 200e6)

        # IDelayCtrl.
        self.idelayctrl = S7IDELAYCTRL(self.cd_idelay)

# BaseSoC -----------------------------------------------------------------------------------------

class BaseSoC(SoCCore):
    SoCCore.csr_map = {
        # SoC.
        "uart"        : 0,
        "icap"        : 1,
        "flash"       : 2,
        "xadc"        : 3,
        "dna"         : 4,

        # PCIe.
        "pcie_phy"    : 10,
        "pcie_msi"    : 11,
        "pcie_dma0"   : 12,

        # XTRX.
        "i2c0"        : 20,
        "i2c1"        : 21,

        # Analyzer.
        "analyzer"    : 30,
    }

    def __init__(self, board="limesdr", sys_clk_freq=int(125e6),
        with_cpu              = True, cpu_firmware=None,
        with_jtagbone         = True,
        with_bscan            = False,
        flash_boot            = False,
        firmware_flash_offset = 0x220000,
    ):
        # Platform ---------------------------------------------------------------------------------
        platform = {
            "fairwaves_cs"  : fairwaves_xtrx_platform.Platform(variant="xc7a35t"),
            "fairwaves_pro" : fairwaves_xtrx_platform.Platform(variant="xc7a50t"),
            "limesdr"       : limesdr_xtrx_platform.Platform()
        }[board]

        if with_bscan:
            from litex.soc.cores.cpu.vexriscv_smp import VexRiscvSMP
            VexRiscvSMP.privileged_debug     = True
            VexRiscvSMP.hardware_breakpoints = 4
            VexRiscvSMP.with_rvc             = True

        # SoCCore ----------------------------------------------------------------------------------
        SoCCore.__init__(self, platform, sys_clk_freq,
            ident                    = f"LiteX SoC on {board.capitalize()} XTRX ",
            ident_version            = True,
            cpu_type                 = "vexriscv_smp" if with_cpu else None,
            cpu_variant              = "standard",
            integrated_rom_size      = 0x8000 if with_cpu else 0,
            integrated_sram_ram_size = 0x1000 if with_cpu else 0,
            integrated_main_ram_size = 0x4000 if with_cpu else 0,
            integrated_main_ram_init = [] if cpu_firmware is None or flash_boot else get_mem_data(cpu_firmware, endianness="little"),
            uart_name                = "crossover",
        )
        # Avoid stalling CPU at startup.
        self.uart.add_auto_tx_flush(sys_clk_freq=sys_clk_freq, timeout=1, interval=128)

        # Clocking ---------------------------------------------------------------------------------
        self.crg = CRG(platform, sys_clk_freq)

        # JTAGBone ---------------------------------------------------------------------------------
        if with_jtagbone:
            self.add_jtagbone()
            platform.add_period_constraint(self.jtagbone_phy.cd_jtag.clk, 1e9/20e6)
            platform.add_false_path_constraints(self.jtagbone_phy.cd_jtag.clk, self.crg.cd_sys.clk)

        # JTAG CPU Debug ---------------------------------------------------------------------------
        if with_bscan:
            self.add_jtag_cpu_debug()

        # Leds -------------------------------------------------------------------------------------
        self.leds = LedChaser(
            pads         = platform.request_all("user_led"),
            sys_clk_freq = sys_clk_freq
        )

        # ICAP -------------------------------------------------------------------------------------
        self.icap = ICAP()
        self.icap.add_reload()
        self.icap.add_timing_constraints(platform, sys_clk_freq, self.crg.cd_sys.clk)

        # SPIFlash ---------------------------------------------------------------------------------
        if flash_boot:
            from litespi.modules import N25Q256A
            from litespi.opcodes import SpiNorFlashOpCodes as Codes
            self.add_spi_flash(mode="1x", module=N25Q256A(Codes.READ_1_1_1), with_master=False)

            # Add ROM linker region --------------------------------------------------------------------
            self.bus.add_region("flash", SoCRegion(
                origin = self.bus.regions["spiflash"].origin + firmware_flash_offset,
                size   = 0x40000, # 256kB
                linker = True)
            )
            # Automatically jump to pre-initialized firmware.
            self.add_constant("FLASH_BOOT_ADDRESS", self.bus.regions["flash"].origin)
        else:
            # Automatically jump to pre-initialized firmware.
            self.add_constant("ROM_BOOT_ADDRESS", self.mem_map["main_ram"])
            self.flash_cs_n = GPIOOut(platform.request("flash_cs_n"))
            self.flash      = S7SPIFlash(platform.request("flash"), sys_clk_freq, 25e6)

        # XADC -------------------------------------------------------------------------------------
        self.xadc = XADC()

        # DNA --------------------------------------------------------------------------------------
        self.dna = DNA()
        self.dna.add_timing_constraints(platform, sys_clk_freq, self.crg.cd_sys.clk)

        # PCIe -------------------------------------------------------------------------------------
        self.pcie_phy = S7PCIEPHY(platform, platform.request(f"pcie_x2"),
            data_width  = 64,
            bar0_size   = 0x20000,
            cd          = "sys",
        )
        self.pcie_phy.update_config({
            "Base_Class_Menu"          : "Wireless_controller",
            "Sub_Class_Interface_Menu" : "RF_controller",
            "Class_Code_Base"          : "0D",
            "Class_Code_Sub"           : "10",
            }
        )
        self.add_pcie(phy=self.pcie_phy, address_width=32, ndmas=1,
            with_dma_buffering    = True, dma_buffering_depth=8192,
            with_dma_loopback     = True,
            with_dma_synchronizer = True,
            with_msi              = True
        )

        # I2C Bus0 ---------------------------------------------------------------------------------
        # - Temperature Sensor (TMP108  @ 0x4a) Lime: (TMP1075 @ 0x4b).
        # - PMIC-LMS           (LP8758  @ 0x60).
        # - VCTCXO DAC         Rev4: (MCP4725 @ 0x62) Rev5: (DAC60501 @ 0x4b) Lime: (AD5693 @ 0x4c).
        self.i2c0 = I2CMaster(pads=platform.request("i2c", 0))

        # I2C Bus1 ---------------------------------------------------------------------------------
        # PMIC-FPGA (LP8758 @ 0x60).
        self.i2c1 = I2CMaster(pads=platform.request("i2c", 1))

        # PMIC-FPGA --------------------------------------------------------------------------------
        # Buck0: 1.0V VCCINT + 1.0V MGTAVCC.
        # Buck1: 1.8V/3.3V VCCIO (DIGPRVDD2/DIGPRVDD3/DIGPRPOC + VDD18_TXBUF of LMS + Bank 0/14/16/34/35 of FPGA).
        # Buck2: 1.2V MGTAVTT + 1.2V VDLMS (VDD12_DIG / VDD_SPI_BUF / DVDD_SXR / DVDD_SXT / DVDD_CGEN).
        # Buck3: 1.8V VCCAUX  + 1.8V VDLMS (VDD18_DIG).

        # PMIC-LMS ---------------------------------------------------------------------------------
        # Buck0: +2.05V (used as input to 1.8V LDO for LMS analog 1.8V).
        # Buck1: +3.3V rail.
        # Buck2: +1.75V (used as input to 1.4V LDO for LMS analog 1.4V).
        # Buck3: +1.5V  (used as input to 1.25V LDO for LMS analog 1.25V).

        # Aux -------------------------------------------------------------------------------------
        self.aux = AUX(platform.request("aux"))

        # Timing Constraints/False Paths -----------------------------------------------------------
        for i in range(4):
            platform.toolchain.pre_placement_commands.append(f"set_clock_groups -group [get_clocks {{{{*s7pciephy_clkout{i}}}}}] -group [get_clocks        dna_clk] -asynchronous")
            platform.toolchain.pre_placement_commands.append(f"set_clock_groups -group [get_clocks {{{{*s7pciephy_clkout{i}}}}}] -group [get_clocks       jtag_clk] -asynchronous")
            platform.toolchain.pre_placement_commands.append(f"set_clock_groups -group [get_clocks {{{{*s7pciephy_clkout{i}}}}}] -group [get_clocks       icap_clk] -asynchronous")

        # Lime Top Level Example -------------------------------------------------------------------

        from gateware.LimeTop import LimeTop

        # Create LimeTop instance.
        self.lime_top = LimeTop(platform)

        # Connect LimeTop's MMAP interface to SoC.
        self.bus.add_slave(name="lime_top_mmap", slave=self.lime_top.mmap, region=SoCRegion(origin=0x3000_000, size=0x1000))

        # Connect LimeTop's Streaming interfaces to PCIe.
        self.comb += [
            self.pcie_dma0.source.connect(self.lime_top.dma_rx, keep={"valid", "ready", "last", "data"}),
            self.lime_top.dma_tx.connect(self.pcie_dma0.sink,   keep={"valid", "ready", "last", "data"}),
        ]

    # JTAG CPU Debug -------------------------------------------------------------------------------

    def add_jtag_cpu_debug(self):
        from litex.soc.cores.jtag import XilinxJTAG
        self.jtag = jtag = XilinxJTAG(XilinxJTAG.get_primitive(self.platform.device), chain=4)
        self.comb += [
            self.cpu.jtag_reset.eq(jtag.reset),
            self.cpu.jtag_capture.eq(jtag.capture),
            self.cpu.jtag_shift.eq(jtag.shift),
            self.cpu.jtag_update.eq(jtag.update),
            self.cpu.jtag_clk.eq(jtag.tck),
            self.cpu.jtag_tdi.eq(jtag.tdi),
            self.cpu.jtag_enable.eq(True),
            jtag.tdo.eq(self.cpu.jtag_tdo),
        ]

        self.cd_jtag = ClockDomain()
        self.comb += ClockSignal("jtag").eq(jtag.tck)
        self.platform.add_period_constraint(self.cd_jtag.clk, 1e9/20e6)
        self.platform.add_false_path_constraints(self.cd_jtag.clk, self.crg.cd_sys.clk)

# Build --------------------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="LiteX SoC on Fairwaves/LimeSDR XTRX.")
    parser.add_argument("--board",   default="fairwaves_pro", help="Select XTRX board.", choices=["fairwaves_cs", "fairwaves_pro", "limesdr"])
    parser.add_argument("--with-bscan",            action="store_true",     help="Enable CPU debug over JTAG."),
    parser.add_argument("--build",                 action="store_true",     help="Build bitstream.")
    parser.add_argument("--load",                  action="store_true",     help="Load bitstream.")
    parser.add_argument("--flash",                 action="store_true",     help="Flash bitstream.")
    parser.add_argument("--cable",                 default="digilent_hs2",  help="JTAG cable.")
    parser.add_argument("--driver",                action="store_true",     help="Generate PCIe driver from LitePCIe (override local version).")
    parser.add_argument("--flash-boot",            action="store_true",     help="Write Firmware in Flash instead of RAM.")
    parser.add_argument("--firmware-flash-offset", default=0x220000,        help="Firmware SPI Flash offset.")
    args = parser.parse_args()

    # Build SoC.
    for run in range(2):
        prepare = (run == 0)
        build   = ((run == 1) & args.build)
        soc = BaseSoC(
            board                 = args.board,
            cpu_firmware          = None if prepare else "firmware/firmware.bin",
            with_jtagbone         = not args.with_bscan,
            with_bscan            = args.with_bscan,
            flash_boot            = args.flash_boot,
            firmware_flash_offset = args.firmware_flash_offset,
        )
        builder = Builder(soc, csr_csv="csr.csv")
        builder.build(run=build)
        if prepare:
            os.system(f"cd firmware && make BUILD_DIR={builder.output_dir} clean all")

    # Generate LitePCIe Driver.
    generate_litepcie_software(soc, "software", use_litepcie_software=args.driver)

    # Load Bistream.
    if args.load:
        prog = soc.platform.create_programmer(cable=args.cable)
        prog.load_bitstream(os.path.join(builder.gateware_dir, soc.build_name + ".bit"))

    # Flash Bitstream.
    if args.flash:
        prog = soc.platform.create_programmer(cable=args.cable)
        prog.flash(0, os.path.join(builder.gateware_dir, soc.build_name + ".bin"))

    # Flash Firmware.
    if args.flash_boot and args.flash:
        from litex.soc.software.crcfbigen import insert_crc
        insert_crc("firmware/firmware.bin", fbi_mode=True, o_filename="firmware/firmware.fbi", little_endian=True)
        prog = soc.platform.create_programmer(cable=args.cable)
        prog.flash(args.firmware_flash_offset, "firmware/firmware.fbi")


if __name__ == "__main__":
    main()
