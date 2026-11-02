# Qualification only: exercise the vendor's recursive Make flag forwarding.
.PHONY: vfio-command
vfio-command: config
	@mkdir -p $(I)/build/drivers/bus/pci
	@$(MAKE) $(DPDK_MAKE_ARGS) -n V=1 drivers/bus/pci_sub \
		RTE_MAKE_SUBTARGET=linux/pci_vfio.o > $(B)/pci-vfio-dry-run.txt
	@cp $(VFIO_PROBE) $(DPDK_SOURCE)/drivers/bus/pci/linux/vfio_api_probe.c
	@$(MAKE) $(DPDK_MAKE_ARGS) V=1 drivers/bus/pci_sub \
		RTE_MAKE_SUBTARGET=linux/vfio_api_probe.o
	@cp $(I)/build/drivers/bus/pci/linux/.vfio_api_probe.o.cmd $(B)/vfio-api-command.txt
