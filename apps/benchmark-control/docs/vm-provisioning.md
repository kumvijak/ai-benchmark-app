# VM Provisioning and Connection

## Supported Connection Modes

The VM Connection tab supports two paths.

1. Create new OCI VM
   - Requires a previously validated OCI authentication profile.
   - Allows only the shapes named in the initial benchmark guide:
     - `VM.Standard.E6.Ax.Flex`
     - `VM.Standard4.Ax.Flex`
     - `BM.Standard.E6.Ax`
     - `BM.Standard4.Ax`
   - Uses guide defaults of 16 OCPU, 64 GB memory, 200 GB boot volume, username `ubuntu`, and Canonical Ubuntu 24.04.
   - Allows the user to override OCPU, memory, and boot volume size.
   - Requires explicit user selections for compute compartment, availability domain, network compartment, VCN, subnet, public IP flag, and image.
   - Requires an SSH public key for instance metadata.
   - Accepts the matching SSH private key when the app should create and test the SSH profile automatically.

2. Use existing VM
   - Requires host, port, username, and uploaded private key file.
   - Does not display uploaded private key content in the browser.
   - Stores the private key encrypted in the local app database.
   - Queues the same SSH setup and health check used by newly created VMs.

## SSH Setup and Readiness

Every VM validation now performs the following setup before marking the profile healthy:

1. Records current root filesystem usage with `df -h /`.
2. Expands the root partition with `growpart` and grows the mounted filesystem with `resize2fs` or `xfs_growfs` so the OS can use the full boot disk.
3. Installs missing Ubuntu packages required by this build: `python3-pip`, `python3-venv`, and `curl`.
4. Installs or repairs CPU-capable `vllm==0.31.0+cpu` in the SSH user's managed virtual environment at `~/.aig-vllm` from the release-specific CPU wheel index.
5. Verifies the pinned vLLM/Torch versions and vendor-specific platform (AMD: ZenTorch/native ZenDNN and ZenCpuPlatform; Intel: CpuPlatform), and `vllm._C` extension import before marking vLLM ready.
6. Installs or repairs `guidellm==0.8.0` in the same managed runtime environment.
7. Stores setup status, detected versions, and root usage after setup in the VM Setup Status table on the VM Connection tab.

Setup uses the selected VM profile and does not substitute hosts, credentials, packages, models, or endpoints.

The VM list also performs a lightweight SSH status refresh every 30 seconds while the UI is open. This periodic check updates `healthy`, `failed`, or `unreachable` status only; it does not run package installation or filesystem expansion.

## OCI Resource Flow

New VM deployment is queued as a `provision_vm` job.

1. The worker loads the saved OCI auth profile for the current app user.
2. The worker launches the instance with the selected image, subnet, public-IP setting, shape, and shape config.
3. The worker waits for the instance to reach `RUNNING`.
4. The worker discovers the primary VNIC, public/private IPs, and attached boot volume.
5. If an SSH private key was provided, the worker creates a VM profile and retries SSH setup plus health validation while cloud-init finishes.
6. The worker stores instance OCID, boot volume OCID, IPs, lifecycle state, and linked VM profile ID.

Destroy is queued as a `destroy_vm` job and calls OCI terminate with `preserve_boot_volume=false`. If the boot volume is still available afterward, the worker requests explicit boot-volume deletion.

## No-Fallback Rules

- The app does not substitute another shape when a selected shape fails.
- The app does not choose a model, endpoint, host, compartment, VCN, subnet, or image silently.
- Ubuntu image options are loaded from OCI for Canonical Ubuntu 24.04 only. The user must choose an image compatible with the selected guide-approved shape.
- Benchmark and runtime work only use user-selected VM and vLLM profiles.


## CPU vendor selection

The default CPU runtime uses vLLM 0.31.0+cpu, PyTorch 2.13.0+cpu and CPU
Triton 3.8.0+cpu on Ubuntu 24.04/Python 3.12. Hardware is detected inside the
VM; another backend is never silently substituted.

- AMD requires exposed AVX-512, ZenTorch 2.13.0.1, native ZenDNN 6.0.1 build
  metadata and native ZenCpuPlatform. The pinned PyPI wheel is installed and
  its native build metadata and binary SHA-256 hashes are recorded.
- Intel requires exposed AVX2 and native CpuPlatform. ZenTorch is not installed
  by Intel setup. Capability evidence includes AVX-512, BF16 and AMX flags.
- Other vendors or missing required instructions fail setup explicitly.

The original approved AMD E6 and Intel Standard4 shapes are available again.
E6 remains the new-VM default. Existing runtime profiles migrate with Intel
AMX kernels disabled. Enable the experimental Intel AMX checkbox explicitly
for small-batch kernels; launch requires an Intel CPU, unquantized BF16 weights, amx_tile and
amx_bf16 flags, and Linux permission for AMX tile data. Compatible weight shapes
are still required by vLLM. Enabling the option does not prove every operator
uses AMX; confirm operator-level behavior separately when measuring it.

Setup and launch share tcmalloc/LLVM OpenMP 18 preload and compile settings.
Healthy managed environments are preserved and GuideLLM uses the same package
constraints. AMD native metadata mismatches fail rather than assume a version.

Rebuild the control container, run full VM validation and restart existing
runtimes before benchmarking. A launch verifier runs inside the server process,
records detected vendor/platform/version/AMX settings and process identity, then
invokes the vLLM CLI. Benchmarks require matching activation evidence from a
live server and include it in job results and GuideLLM artifact labels.
BF16 remains the default; validate quantized model paths separately.
