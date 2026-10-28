# nx789j (Red Magic 10 Pro) Mu-Silicium package

`files/` goes over a Mu-Silicium tree that has `uefi/mu-silicium.patch` and `uefi/files/` applied
(same relative paths). Two more pieces are not kept here:

- `Binaries/nx789j/`: a copy of Mu-Silicium's `Binaries/dodge/` (OnePlus 13, same SoC; the K90 port
  runs on the same drivers);
- `Resources/DTBs/nx789j.dtb`: `design/nx789j/nx789j-live.dtb` (the phone's `/sys/firmware/fdt`).

Build: `python3 build_uefi.py -d nx789j -r RELEASE` in Mu-Silicium (venv-mu).

The memory map is dodge's with two changes for this phone: `DXE_Heap` ends at 0xD4E23000, where the
no-map `hwfence-shmem` starts, and `XNU_Payload` (0xF0000000-0xF3800000, below `trust_ui_vm_region`)
is reserved for the BootShim's payload copy, as in the K90's map.
