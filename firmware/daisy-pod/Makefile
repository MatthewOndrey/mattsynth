# PanelLink - Daisy Pod side of the panel link.
# Layout follows DaisyExamples/pod/<example>: point the two library paths at the
# vendored checkouts and include libDaisy's generic core Makefile.

TARGET = PanelLink


# internal flash build
# blocking new features. Needs the Daisy bootloader in internal flash.
# APP_TYPE unset: internal flash build (bootloader path reverted)

CPP_SOURCES = PanelLink.cpp

LIBDAISY_DIR   = ../../vendor/libDaisy
DAISYSP_DIR    = ../../vendor/DaisySP
LINK_DIR       = ../../link

# canonical protocol header lives in link/, shared with the panel component
C_INCLUDES += -I$(LINK_DIR)

# the FM engine is the hot path: -O3 and fast-math buy real headroom on the M7
# -Os buys ~8 KB of flash (126448 -> 118044). The DSP loop is hand-written
# float code; if the audio callback gets slower, put -O3 back and trim instead.
OPT = -Os

SYSTEM_FILES_DIR = $(LIBDAISY_DIR)/core
include $(SYSTEM_FILES_DIR)/Makefile
