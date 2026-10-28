#ifndef _XNU_LOADER_H_
#define _XNU_LOADER_H_

//
// The boot shim copies everything appended after the UEFI FD to this address.
// The annibale MemoryMapLib "XNU_Payload" entry reserves the whole window
// [XNU_PAYLOAD_BASE, XNU_PAYLOAD_BASE + XNU_PAYLOAD_REGION_SIZE) in PEI,
// before DXE can allocate over the shim's copy. The container may occupy at
// most XNU_PAYLOAD_MAX_SIZE bytes counting the 8-byte header; the 1 MiB tail
// stays reserved for the panic log at the window end. The window ends where
// the DTB trust_ui_vm_region static reservation begins. These are the
// annibale values: the memory map entry, the packer, the boot shim and the
// phone loader profile must carry the same numbers;
// tools/test-annibale-payload-contract.py checks them against the map and
// the embedded DTB.
//
#define XNU_PAYLOAD_BASE         0xF0000000ULL
#define XNU_PAYLOAD_REGION_SIZE  0x03800000ULL // reserved window, mirrors the MemoryMapLib entry
#define XNU_PAYLOAD_MAX_SIZE     0x03700000ULL // max container incl. header; window minus 1 MiB panic tail
#define XNU_PAYLOAD_MAGIC        0x50554E58 // "XNUP"

typedef struct {
  UINT32 Magic;
  UINT32 Size;
} XNU_PAYLOAD_HEADER;

// The header is followed by entries; an empty name ends the list. An entry
// whose padded data does not fit in the remaining container bytes is invalid.
typedef struct {
  CHAR8  Name[16];
  UINT32 Size;
  UINT32 Reserved;
  // UINT8 Data[Size], padded to 16 bytes
} XNU_PAYLOAD_ENTRY;

//
// Starts loader.efi from the payload region. Does not return: every outcome
// ends in the local halt defined in XnuLoader.c.
//
__attribute__((noreturn))
VOID
XnuLoaderMain ();

#endif /* _XNU_LOADER_H_ */
