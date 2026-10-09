from __future__ import annotations

import os
from pathlib import Path
import tempfile
from typing import Any


DOC_APPROVED_SHAPES = (
    "VM.Standard.E6.Ax.Flex",
    "VM.Standard4.Ax.Flex",
    "BM.Standard.E6.Ax",
    "BM.Standard4.Ax",
)

DEFAULT_VM_DEPLOYMENT = {
    "ocpus": 16.0,
    "memory_gb": 64.0,
    "boot_volume_gb": 200,
    "username": "ubuntu",
    "operating_system": "Canonical Ubuntu",
    "operating_system_version": "24.04",
}


class OciContext:
    def __init__(self, config: dict[str, Any] | None, signer: Any | None, temp_dir: tempfile.TemporaryDirectory[str] | None = None):
        self.config = config or {}
        self.signer = signer
        self._temp_dir = temp_dir

    def close(self) -> None:
        if self._temp_dir:
            self._temp_dir.cleanup()

    def __enter__(self) -> "OciContext":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


def context_from_saved_auth(row: dict[str, Any], decrypt) -> OciContext:
    import oci

    if row["mode"] == "instance_principal":
        signer = principal_signer()
        return OciContext(config={}, signer=signer)

    profile_text = decrypt(row.get("encrypted_profile_text")) or ""
    private_key = decrypt(row.get("encrypted_private_key")) or ""
    if not profile_text or not private_key:
        raise RuntimeError("Saved user-profile authentication is missing config text or private key")
    profile_name = row.get("profile_name")
    if not profile_name:
        raise RuntimeError("Saved user-profile authentication is missing profile name")

    temp_dir = tempfile.TemporaryDirectory(prefix="aig-oci-auth-")
    tmp = Path(temp_dir.name)
    config_path = tmp / "config"
    key_path = tmp / "oci_api_key.pem"
    key_path.write_text(private_key)
    os.chmod(key_path, 0o600)
    config_text = profile_text_with_key_file(profile_text, profile_name, key_path)
    config_path.write_text(config_text)
    config = oci.config.from_file(str(config_path), profile_name)
    oci.config.validate_config(config)
    return OciContext(config=config, signer=None, temp_dir=temp_dir)


def profile_text_with_key_file(profile_text: str, profile_name: str, key_path: Path) -> str:
    lines = profile_text.splitlines()
    selected_header = f"[{profile_name}]"
    output: list[str] = []
    in_selected = False
    found_selected = False
    inserted = False

    for line in lines:
        stripped = line.strip()
        is_header = stripped.startswith("[") and stripped.endswith("]")
        if is_header:
            if in_selected and not inserted:
                output.append(f"key_file={key_path}")
                inserted = True
            in_selected = stripped == selected_header
            found_selected = found_selected or in_selected
        if in_selected and stripped.startswith("key_file"):
            continue
        output.append(line)

    if found_selected and not inserted:
        output.append(f"key_file={key_path}")
    elif not found_selected:
        if output and output[-1].strip():
            output.append("")
        output.append(selected_header)
        output.append(f"key_file={key_path}")
    return "\n".join(output).rstrip() + "\n"


def clients(ctx: OciContext) -> dict[str, Any]:
    import oci

    kwargs = {"signer": ctx.signer} if ctx.signer else {}
    return {
        "identity": oci.identity.IdentityClient(ctx.config, **kwargs),
        "compute": oci.core.ComputeClient(ctx.config, **kwargs),
        "network": oci.core.VirtualNetworkClient(ctx.config, **kwargs),
        "block": oci.core.BlockstorageClient(ctx.config, **kwargs),
    }


def principal_signer() -> Any:
    import oci

    if os.getenv("OCI_RESOURCE_PRINCIPAL_VERSION"):
        return oci.auth.signers.get_resource_principals_signer()
    return oci.auth.signers.InstancePrincipalsSecurityTokenSigner()


def signer_tenancy_id(signer: Any) -> str | None:
    for attr in ("tenancy_id", "tenancy_id_from_token"):
        value = getattr(signer, attr, None)
        if value:
            return value
    claims = getattr(signer, "claims", None)
    if isinstance(claims, dict):
        return claims.get("res_tenant") or claims.get("tenant")
    return None


def list_accessible_compartments(identity: Any, tenancy_id: str) -> list[dict[str, Any]]:
    import oci

    root = identity.get_compartment(tenancy_id).data
    rows = [{
        "id": tenancy_id,
        "name": getattr(root, "name", "root"),
        "description": getattr(root, "description", ""),
        "lifecycle_state": "ACTIVE",
        "parent_compartment_id": None,
    }]
    compartments = oci.pagination.list_call_get_all_results(
        identity.list_compartments,
        tenancy_id,
        compartment_id_in_subtree=True,
        access_level="ACCESSIBLE",
    ).data
    for comp in compartments:
        if getattr(comp, "lifecycle_state", None) != "ACTIVE":
            continue
        rows.append({
            "id": comp.id,
            "name": comp.name,
            "description": getattr(comp, "description", ""),
            "lifecycle_state": comp.lifecycle_state,
            "parent_compartment_id": getattr(comp, "compartment_id", None),
        })
    return rows


def ubuntu_2404_images(compute: Any, compartment_id: str) -> list[dict[str, Any]]:
    import oci

    images = oci.pagination.list_call_get_all_results(
        compute.list_images,
        compartment_id,
        operating_system=DEFAULT_VM_DEPLOYMENT["operating_system"],
        operating_system_version=DEFAULT_VM_DEPLOYMENT["operating_system_version"],
        sort_by="TIMECREATED",
        sort_order="DESC",
    ).data
    return [{
        "id": image.id,
        "display_name": image.display_name,
        "operating_system": image.operating_system,
        "operating_system_version": image.operating_system_version,
        "time_created": str(image.time_created),
    } for image in images]


def shape_config_details(shape: str, ocpus: float, memory_gb: float) -> Any | None:
    if not shape.endswith(".Flex"):
        return None
    import oci

    return oci.core.models.LaunchInstanceShapeConfigDetails(
        ocpus=ocpus,
        memory_in_gbs=memory_gb,
    )
