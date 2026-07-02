# mks_send.py

A tiny standalone command-line tool (no Cura, no extra Python packages)
that sends a .gco/.gcode file to an MKS Robin WiFi / MKS TFT WiFi
board over your local network. Protocol extracted from the official
MKS WiFi Plugin for Cura.

Works the same on Windows, macOS, or Linux — pure Python standard
library, nothing to install beyond Python itself.

You can add it in "Output options" on PrusaSlicer, just dont 

# Usage on macOS #

### Download and place in a known folder, then make it excecutable: ###
 
 - chmod +x ~/_your-folder_/mks_send.py

### Then use it like this: ###

#### On terminal: ####

 - mks_send.py <printer-ip> [options] [path-to-gcode-file]

#### PrusaSlicer: ####

You can add it in "Output options", just don't add the gcode file name, the slicer will do it for you.

# Options #

* **--start-print**
  - After upload, send M23 <file> + M24 to start the print
* **--http-port**
  - HTTP port used for the upload itself (default 80)
* **--remote-name**
  - Name to give the file on the printer's SD card (default: same as local filename)
* **--timeout**
  - Network timeout in seconds (default 15)

## How to compile #
No need to compile!

### THIS IS MY FIRST CONTRIBUTION, NOT AN EXPERT. BE NICE AND IF SOMETHING IS WRONG JUST LET ME KNOW! ###
