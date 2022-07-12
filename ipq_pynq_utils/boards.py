import spidev

try:
    import xrfdc
except:
    # Probably on development system, ignore error for now
    pass

from .clock_models import CLK104
from . import utils

# Import compat-lib for python versions older than 3.10
if utils.python_version_lt("3.10.0"):
    from importlib_resources import files
else:
    from importlib.resources import files

"""
Board utilities module. This module is responsible for performing board-specific
opertions like configuration of the clock sources, clock alignment, multi-tile
synchronization and the likes.
"""

# For multi-tile synchronization the cffi.FFI instance of xrfdc is hijacked
# and populated with the missing cdefs in _XRFDC_CDEFS. Additionally, a bunch
# of constants are defined in the local context to be used for interaction with
# those cdefs.
_cdefs_loaded = False

XRFDC_MTS_SYSREF_DISABLE = 0
XRFDC_MTS_SYSREF_ENABLE = 1

XRFDC_MTS_SCAN_INIT = 0
XRFDC_MTS_SCAN_RELOAD = 1

XRFDC_MTS_OK = 0
XRFDC_MTS_NOT_SUPPORTED = 1
XRFDC_MTS_TIMEOUT = 2
XRFDC_MTS_MARKER_RUN = 4
XRFDC_MTS_MARKER_MISM = 8
XRFDC_MTS_DELAY_OVER = 16
XRFDC_MTS_TARGET_LOW = 32
XRFDC_MTS_IP_NOT_READY = 64
XRFDC_MTS_DTC_INVALID = 128
XRFDC_MTS_NOT_ENABLED = 512
XRFDC_MTS_SYSREF_GATE_ERROR = 2048
XRFDC_MTS_SYSREF_FREQ_NDONE = 4096
XRFDC_MTS_BAD_REF_TILE = 8192

XRFDC_MTS_DAC = None
XRFDC_MTS_ADC = None

def read_datafile(s):
    with files("ipq_pynq_utils").joinpath("data/" + s).open() as f:
        return f.read()

class RFTileConfig:
    def __init__(self, parent, typename, i):
        self.parent = parent
        self.params = parent.params
        self.name = f"{typename}{i}"
        self.base = f"{typename}{i}_"
        self.i = i

        self.enabled = bool(int(self.ga("Enable")))
        self.refclk_freq = float(self.ga("Refclk_Freq"))
        self.src = int(self.ga("Clock_Source"))
        self.pll_enable = self.gb("PLL_Enable")
        self.fabric_clk = float(self.ga("Fabric_Freq"))
        self.mts = self.gb("Multi_Tile_Sync")
        self.clk_distribution = self.gb("Clock_Dist")

    def ga(self, n):
        return self.params[self.base + n]

    def gb(self, n):
        v = self.ga(n).lower()
        if v == "false":
            return False
        elif v == "true":
            return True

        return bool(int(v))

    def print_line(self):
        print(f" {self.name:4} | {str(self.enabled):6} | {self.refclk_freq:9.2f} | {self.parent.rftiles[self.src].name:12s} | {str(self.pll_enable):10s} | {self.fabric_clk:10.2f} | {str(self.mts):5s} | {str(self.clk_distribution):5s}")

    def __str__(self):
        return f"{self.name}(enabled={self.enabled}, freq={self.freq}, src={self.src}, pll_enable={self.pll_enable}, fabric_clk={self.fabric_clk}, mts={self.mts}, clk_distribution={self.clk_distribution})"

    def __repr__(self):
        return str(self)

class RFDCConfig:
    def __init__(self, ol, name):
        self.params = ol.ip_dict[name]["parameters"]
        self.rftiles = []

        for i in range(4):
            self.rftiles.append(RFTileConfig(self, "ADC", i))

        for i in range(4):
            self.rftiles.append(RFTileConfig(self, "DAC", i))

    def print_table(self):
        print(f" Name | Enable | Ref Clock | Clock Source | PLL Enable | Fabric CLK |  MTS  | Clock Dist")
        print( "------|--------|-----------|--------------|------------|------------|-------|------------")
        for tile in self.rftiles:
            tile.print_line()

    def get_clk_requirements(self, hide_warnings=False):
        ret = {
            "RF_CLKO_ADC": None,
            "RF_CLKO_DAC": None,
            "ADC_REFCLK": None,
            "DAC_REFCLK": None,
            "PL_CLK": None,
            "MTS": False
        }

        pl_clk_missmatch = False

        adc1 = self.rftiles[1]
        if adc1.enabled and adc1.src == 1:
            ret["RF_CLKO_DAC"] = adc1.refclk_freq

        dac2 = self.rftiles[6]
        if dac2.enabled and dac2.src == 6:
            ret["RF_CLKO_DAC"] = dac2.refclk_freq

        adc2 = self.rftiles[2]
        if adc2.enabled and adc2.src == 2:
            ret["ADC_REFCLK"] = adc2.refclk_freq

        dac0 = self.rftiles[4]
        if dac0.enabled and dac0.src == 4:
            ret["DAC_REFCLK"] = dac0.refclk_freq

        for tile in self.rftiles:
            if not tile.enabled:
                continue

            if tile.mts:
                ret["MTS"] = True

            if pl_clk_missmatch:
                continue

            if ret["PL_CLK"] == None:
                ret["PL_CLK"] = tile.fabric_clk

            elif ret["PL_CLK"] != tile.fabric_clk:
                if not hide_warnings:
                    print("\x1b[31mWARNING:\x1b[0m Fabric clock missmatch between RF tiles. This is not generally a problem, but this means that a device-internal PLL will be necessary to generate the required clocks and a PL_CLK rate must be specified manually!")

                pl_clk_missmatch = True
                ret["PL_CLK"] = None

        return ret

    def get_mts_bits(self):
        ADCs = 0
        DACs = 0

        for i in range(4):
            ADCs |= int(self.rftiles[i].enabled and self.rftiles[i].mts) * (1 << i)

        for i in range(4):
            DACs |= int(self.rftiles[4+i].enabled and self.rftiles[4+i].mts) * (1 << i)

        return ADCs, DACs

class ZCU208Board:
    """
    The ZCU208 board is equipped with a ZU48DR and a CLK104 board as its clock source.
    To load the PLLs on the CLK104, three SPI devices need to be accessed which are
    made available by the operating system: /dev/spidev1.1 - /dev/spidev1.3. These
    are not directly connected spi devices, but rather through a multilplexed I2C bus
    and an I2C to SPI bridge.

    Registers of the SPI device are written by taking the 24-bit value from the register
    file (Which itself consists of a 16-bit address and a 8-bit register value),
    split them into a list of three bytes and write those consecutively on the spi bus.
    Ideally the SPI device expects a full 24-bit word to be written, but deals gracefully
    with that word being split up into three 8-bit words.

    The second function of the ZCU208Board class is to provide Multi-Tile synchronization
    of the dataconverter tiles. Note that compatible clocks must have been configured, MTS
    synchronization must have been enabled in the IP configurator and the PL SYSREF line
    must have been connected to the Dataconverter IP for MTS to work.
    As a short introduction, MTS works by aligning the dataconverters and digitial signal
    chains to a low-frequency SYSREF signal, which is a periodic signal < 10 MHz signal
    that's locked to the main PLLs, and should arrive at all relevant converters and
    chips simultaenously. This way digital delays can be measured and compensated, NCO
    phases can be reset and PLLs clock division ambigiuity can be eliminated by resetting
    at a leading edge of the sysref pulses. Because the alignment of the sysref signal
    is integral to this process, it *MUST* be generated with certain restrictions in mind,
    for more information see "SYSREF Signal Requirements" in "Zynq UltraScale+ RFSoC
    RF Data Converter v2.6 Gen 1/2/3 LogiCORE IP Product Guide".
    """

    def __init__(self, lmk_srcfile=None):
        global _cdefs_loaded,XRFDC_MTS_DAC,XRFDC_MTS_ADC

        self.spi_lmk = spidev.SpiDev(1, 1)
        self.spi_dac = spidev.SpiDev(1, 2)
        self.spi_adc = spidev.SpiDev(1, 3)

        for spi in [self.spi_lmk, self.spi_dac, self.spi_adc]:
            spi.bits_per_word = 8
            spi.max_speed_hz = 100000

        if not _cdefs_loaded:
            xrfdc._ffi.cdef(read_datafile("xrfdc_cdefs.h"))
            _cdefs_loaded = True

        XRFDC_MTS_DAC = xrfdc._lib.XRFDC_DAC_TILE
        XRFDC_MTS_ADC = xrfdc._lib.XRFDC_ADC_TILE

        self.clk104 = CLK104(lmk_srcfile)

    def _parse_register_file(filename):
        try:
            lines = read_datafile(f"clockFiles/{filename}").strip().split("\n")
        except:
            with open(filename, "r") as f:
                lines = f.read().strip().split("\n")

        ret = []

        for line in lines:
            a,b = line.split("\t")
            data = int(b, 16)

            ret.append(data)

        return ret

    def _write_registers(spi, registers):
        for word in registers:
            data = [(word >> 16) & 0xFF, (word >> 8) & 0xFF, word & 0xFF]
            spi.writebytes(data)

    def configure(self, overlay, lmxdac=None, lmxadc=None, pl_clk=None, download=True):
        self.overlay = overlay
        self.rfdc_name = None
        for key in overlay.ip_dict.keys():
            if key.startswith("usp_rf_data_converter_0"):
                self.rfdc_name = key
                break

        if self.rfdc_name is None:
            raise RuntimeError("No RF Data Converter present in overlay!")

        self.rfdc_config = RFDCConfig(overlay, self.rfdc_name)
        self.rfdc_config.print_table()
        clkreqs = self.rfdc_config.get_clk_requirements()
        print("Clock Requirements:", clkreqs)

        mts = clkreqs["MTS"]

        if pl_clk is not None:
            f = pl_clk
        elif clkreqs["PL_CLK"] is not None:
            f = clkreqs["PL_CLK"]
        else:
            raise RuntimeError("A single PL CLK rate could not be determined, please provide one manually using the pl_clk parameter to ZCU208Board.configure()!")

        en_adc_pll = clkreqs["RF_CLKO_ADC"] is not None
        self.clk104.RF_PLL_ADC_REF.enable = en_adc_pll
        self.clk104.RF_PLL_ADC_REF.sysref_enable = en_adc_pll and mts

        en_dac_pll = clkreqs["RF_CLKO_DAC"] is not None
        self.clk104.RF_PLL_ADC_REF.enable = en_dac_pll
        self.clk104.RF_PLL_ADC_REF.sysref_enable = en_dac_pll and mts

        en = clkreqs["ADC_REFCLK"] is not None
        self.clk104.ADC_REFCLK.enable = en
        self.clk104.ADC_REFCLK.sysref_enable = en and mts

        if en:
            self.clk104.ADC_REFCLK.freq = clkreqs["ADC_REFCLK"]

        en = clkreqs["DAC_REFCLK"] is not None
        self.clk104.DAC_REFCLK.enable = en
        self.clk104.DAC_REFCLK.sysref_enable = en and mts

        if en:
            self.clk104.DAC_REFCLK.freq = clkreqs["DAC_REFCLK"]

        ZCU208Board._write_registers(self.spi_lmk, [0x000090] + self.clk104.lmk.get_register_dump())

        if en_adc_pll:
            f = clkreqs["RF_CLKO_ADC"]
            if lmxadc is not None:
                ZCU208Board._write_registers(self.spi_adc, ZCU208Board._parse_register_file(lmxadc))
            else:
                self.lmx_adc.set_output_frequency(f)
                ZCU208Board._write_registers(self.spi_adc, self.clk104.lmx_adc.get_register_dump())

        if en_dac_pll:
            f = clkreqs["RF_CLKO_DAC"]
            if lmxdac is not None:
                ZCU208Board._write_registers(self.spi_dac, ZCU208Board._parse_register_file(lmxdac))
            else:
                self.lmx_dac.set_output_frequency(f)
                ZCU208Board._write_registers(self.spi_dac, self.clk104.lmx_dac.get_register_dump())

        if not download:
            return

        print("Configured clocks, loading bitstream ... ", end="")
        overlay.download()
        print("done!")

        if mts:
            print("Performing MTS ... ", end="")

            ADCs, DACs = self.rfdc_config.get_mts_bits()

            if ADCs != 0:
                ret = self.perform_mts(getattr(overlay, self.rfdc_name), XRFDC_MTS_ADC, tiles=ADCs)

                if ret != XRFDC_MTS_OK:
                    raise RuntimeError(f"Failed to perform ADC MTS: 0x{ret:08x}")

            if DACs != 0:
                ret = self.perform_mts(getattr(overlay, self.rfdc_name), XRFDC_MTS_DAC, tiles=DACs)

                if ret != XRFDC_MTS_OK:
                    raise RuntimeError(f"Failed to perform DAC MTS: 0x{ret:08x}")

            print("done!")

    def print_clock_summary(self):
        print(f"Name            |  Frequency  | Enabled | Sysref Enabled")
        print( "----------------|-------------|---------|----------------")
        print(f"PLL2_FREQ       | {self.clk104.PLL2_FREQ:7.2f} MHz | True    |")
        print(f"AMS_SYSREF      | {self.clk104.SYSREF_FREQ:7.2f} MHz |         | {str(self.clk104.AMS_SYSREF.sysref_enable)}")
        print(f"SYSREF_FREQ     | {self.clk104.SYSREF_FREQ:7.2f} MHz |         | True")
        print(f"ADC_REFCLK      | {self.clk104.ADC_REFCLK.freq:7.2f} MHz | {str(self.clk104.ADC_REFCLK.enable):<7} | {str(self.clk104.ADC_REFCLK.sysref_enable):<7}")
        print(f"DAC_REFCLK      | {self.clk104.DAC_REFCLK.freq:7.2f} MHz | {str(self.clk104.DAC_REFCLK.enable):<7} | {str(self.clk104.DAC_REFCLK.sysref_enable):<7}")
        print(f"RF_PLL_ADC_REF  | {self.clk104.RF_PLL_ADC_REF.freq:7.2f} MHz | {str(self.clk104.RF_PLL_ADC_REF.enable):<7} | {str(self.clk104.RF_PLL_ADC_REF.sysref_enable):<7}")
        print(f"RF_PLL_DAC_REF  | {self.clk104.RF_PLL_DAC_REF.freq:7.2f} MHz | {str(self.clk104.RF_PLL_DAC_REF.enable):<7} | {str(self.clk104.RF_PLL_DAC_REF.sysref_enable):<7}")
        print(f"PL_CLK          | {self.clk104.PL_CLK.freq:7.2f} MHz | {str(self.clk104.PL_CLK.enable):<7} | {str(self.clk104.PL_CLK.sysref_enable):<7}")

    def perform_mts(self, rfdc, tile_type, tiles=0xf, target_latency=-1):
        """
        Perform multi-tile synchronization.

        rfdc: This is the xrfdc.RFdc object which is created as part of the overlay.
        tiles: A bitmap of tiles to perform synchronization on, where the zero-th bit refers to the first tile and so on.
               To synchronize the first three tiles, pass 0x7 (or 0b111).
        tile_type:   Whether to synchronize DACs (XRFDC_MTS_DAC) or ADCs (XRFDC_MTS_ADC).
        """

        sync_config = xrfdc._ffi.new("XRFdc_MultiConverter_Sync_Config*")

        ret = xrfdc._lib.XRFdc_MultiConverter_Init(sync_config, xrfdc._ffi.NULL, xrfdc._ffi.NULL, 0)

        sync_config.Tiles = tiles
        sync_config.Target_Latency = target_latency

        status_dac = xrfdc._lib.XRFdc_MultiConverter_Sync(rfdc._instance, tile_type, sync_config);
        if status_dac != XRFDC_MTS_OK:
            raise RuntimeError(f"Failed to perform MTS: {hex(status_dac)}")

        marker_delay = sync_config.Marker_Delay
        offsets = list(sync_config.Offset)
        latency = list(sync_config.Latency)

        del sync_config

        return marker_delay, offsets, latency

