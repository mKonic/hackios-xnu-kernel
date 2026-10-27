/* User-client serialization and the fields whose shared-page offsets vary by ABI. */
IOReturn
IOSurfaceRootUserClient::returnSurface(struct minisurf_client_ref * ref, bool known, IOExternalMethodArguments * args)
{
	MiniIOSurface * s = ref->surface;
	struct minisurf_result result = s->info;
	mach_vm_address_t address = ref->map->getAddress();

	result.shared = address;
	result.aux = address + MINISURF_AUX_OFFSET;
	result.base = known ? ~0ULL : s->client == NULL ? address + PAGE_SIZE :
	    ref->pixelMap != NULL ? ref->pixelMap->getAddress() : s->clientAddress;
	uint32_t size = minisurfWriteResult(args->structureOutput, args->structureOutputSize, fABI, &result, s->planes);
	if (size == 0) {
		return kIOReturnBadArgument;
	}
	args->structureOutputSize = size;
	return kIOReturnSuccess;
}

IOReturn
IOSurfaceRootUserClient::methodInit(IOExternalMethodArguments * args)
{
	minisurf_abi abi = minisurfInitABI(args->structureOutputSize);

	IOLockLock(gSurfaceLock);
	if ((fABI != MINISURF_UNCLAIMED && fABI != abi) || !minisurfInitReply(args->structureOutput,
	    args->structureOutputSize, (uint32_t) PAGE_MASK, MINISURF_ROW_ALIGN - 1, MINISURF_MAX_DIMENSION)) {
		IOLockUnlock(gSurfaceLock);
		return kIOReturnBadArgument;
	}
	fABI = abi;
	IOLockUnlock(gSurfaceLock);
	return kIOReturnSuccess;
}

IOReturn
IOSurfaceRootUserClient::methodSetBulkAttachments(const uint8_t * in, size_t inSize)
{
	uint64_t size, mask;
	uint32_t sid;
	struct minisurf_client_ref * ref;
	IOReturn ret = kIOReturnNotFound;

	IOLockLock(gSurfaceLock);
	if (!minisurfBulkInput(fABI, in, inSize, &size, &mask, &sid)) {
		IOLockUnlock(gSurfaceLock);
		return kIOReturnBadArgument;
	}
	ref = findRef(sid, false);
	if (ref != NULL) {
		minisurfApplyBulk(ref->surface->shared(), fABI, in, size, mask);
		ret = kIOReturnSuccess;
	}
	IOLockUnlock(gSurfaceLock);
	return ret;
}
