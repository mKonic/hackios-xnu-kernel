#!/usr/bin/env python3
"""Host test for uefi/export.sh's preflight (review 2026-10-24, U2).

Running export.sh on the current Mu checkout used to rewrite BASE to a local
commit, shrink mu-silicium.patch to the uncommitted diff and delete the uefi/
overlay (files/) before the first cp for the missing redwoodPkg failed. The
script now refuses before writing anything. These cases run the real script
against throwaway git fixtures only -- never against the working overlay:

  annibale_tree  HEAD ahead of BASE and no redwood files (today's real tree)
                 -> refused; BASE, mu-silicium.patch and the files/ sentinels
                    keep their bytes;
  missing_mu     ../Mu-Silicium absent -> refused, overlay intact;
  partial_port   HEAD at BASE but the port files absent -> refused before
                 rm -rf (the old script deleted files/ at exactly that cp);
  staged_edit    apply.sh shape with the BootShim edit staged -> refused
                 (git diff would drop it from the patch); the pre-preflight
                 script exported rc 0 with an empty patch over this same
                 fixture, reproduced here as a contrast run;
  wrong_type     redwoodPkg present as a regular file -> refused (the old
                 script exported the wrong shape rc 0, also contrasted);
  apply_shape    HEAD at BASE, uncommitted upstream edit, untracked port
                 files -> exports BASE, patch and files/ as designed.

The fixture HEAD is a fresh hash, so the two HEAD-at-BASE cases rewrite the
fixture BASE to that hash; the fixture never touches the real uefi/ files.
The contrast runs use the pre-preflight export.sh from kernel git HEAD, or
$XNU_UEFI_EXPORT_OLD when there is no git (frozen snapshots); without either
the two contrast cases are skipped and reported.
Usage: python3 tools/test-uefi-export.py"""

import os
import shutil
import subprocess
import sys
import tempfile

KERNEL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXPORT = os.path.join(KERNEL, "uefi", "export.sh")
BASE_SHA = "e51cd877e31c8fbb322032dc4cb6715e83d25ad3"

PORT_FILES = [
    "Platforms/Xiaomi/redwoodPkg/redwoodPkg.dec",
    "Resources/Configs/redwood.toml",
    "Resources/DTBs/redwood.dtb",
    "Silicon/Silicium/SiliciumPkg/Library/BootManagerLib/XnuLoader.c",
    "Silicon/Silicium/SiliciumPkg/Library/BootManagerLib/XnuLoader.h",
    "Binaries/redwood/README.md",
]

fails = []


def ck(cond, name, extra=""):
    if not cond:
        fails.append("FAIL %s%s" % (name, ("\n" + extra) if extra else ""))


def git(repo, *args):
    subprocess.run(["git", "-c", "user.email=t@t.invalid", "-c", "user.name=t"] + list(args),
                   cwd=repo, check=True, capture_output=True)


def make_uefi(tmp):
    """Fixture copy of the uefi/ directory: real script, BASE, sentinel overlay."""
    uefi = os.path.join(tmp, "uefi")
    os.makedirs(os.path.join(uefi, "files", "Platforms", "Xiaomi"))
    shutil.copy(EXPORT, os.path.join(uefi, "export.sh"))
    with open(os.path.join(uefi, "BASE"), "w") as f:
        f.write(BASE_SHA + "\n")
    with open(os.path.join(uefi, "mu-silicium.patch"), "w") as f:
        f.write("original patch bytes\n")
    with open(os.path.join(uefi, "files", "Platforms", "Xiaomi", "sentinel.txt"), "w") as f:
        f.write("overlay sentinel\n")
    return uefi


def make_mu(tmp, local_commit, with_port):
    """Throwaway Mu-Silicium stand-in; returns its HEAD hash."""
    mu = os.path.join(tmp, "Mu-Silicium")
    os.makedirs(mu)
    git(mu, "init", "-q", ".")
    for path in ("BootShim/BootShim.S", "Silicon/Silicium/SiliciumPkg/Library/BootManagerLib/BootManager.c"):
        p = os.path.join(mu, path)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write("upstream bytes\n")
    git(mu, "add", "-A")
    git(mu, "commit", "-qm", "upstream")
    if with_port:
        for path in PORT_FILES:
            p = os.path.join(mu, path)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write("port bytes: %s\n" % path)
    if local_commit:  # annibale reality: the port files sit in a local commit, not untracked
        git(mu, "add", "-A")
        git(mu, "commit", "-qm", "local commit on top of the export base")
    return subprocess.run(["git", "-C", mu, "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()


def set_base(uefi, sha):
    with open(os.path.join(uefi, "BASE"), "w") as f:
        f.write(sha + "\n")


def run_export(uefi):
    return subprocess.run(["bash", os.path.join(uefi, "export.sh")], capture_output=True, text=True)


def old_export_text():
    """The pre-preflight export.sh from kernel git HEAD, for contrast runs that
    show the old script silently losing the same inputs the guard refuses;
    $XNU_UEFI_EXPORT_OLD points at a copy for frozen snapshots without git."""
    src = os.environ.get("XNU_UEFI_EXPORT_OLD")
    if src:
        try:
            with open(src, "rb") as f:
                return f.read().decode()
        except OSError as exc:
            sys.exit("cannot read XNU_UEFI_EXPORT_OLD=%s: %s" % (src, exc))
    r = subprocess.run(["git", "-C", KERNEL, "show", "HEAD:uefi/export.sh"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def run_script(uefi, name, text):
    path = os.path.join(uefi, name)
    with open(path, "w") as f:
        f.write(text)
    return subprocess.run(["bash", path], capture_output=True, text=True)


def stage_bootshim(mu):
    with open(os.path.join(mu, "BootShim", "BootShim.S"), "w") as f:
        f.write("patched boot shim\n")
    git(mu, "add", "BootShim/BootShim.S")


def break_pkg_dir(mu):
    pkg = os.path.join(mu, "Platforms", "Xiaomi", "redwoodPkg")
    shutil.rmtree(pkg)
    with open(pkg, "w") as f:
        f.write("a regular file where a directory belongs\n")


def read(path):
    with open(path, "rb") as f:
        return f.read()


def overlay_untouched(uefi, tag, base=BASE_SHA):
    ck(read(os.path.join(uefi, "BASE")).decode().strip() == base, tag + "_BASE_intact")
    ck(read(os.path.join(uefi, "mu-silicium.patch")) == b"original patch bytes\n", tag + "_patch_intact")
    ck(read(os.path.join(uefi, "files", "Platforms", "Xiaomi", "sentinel.txt")) == b"overlay sentinel\n",
       tag + "_overlay_sentinel_intact")


def refused(tmp, tag, stderr_needle, base=None):
    uefi = make_uefi(tmp)
    if base:
        set_base(uefi, base)  # fixture HEAD is this hash: the refusal must come from a later check
    r = run_export(uefi)
    ck(r.returncode != 0, tag + "_refused", r.stdout + r.stderr)
    ck("uefi/export.sh:" in r.stderr, tag + "_explains_refusal", r.stderr)
    if stderr_needle:
        ck(stderr_needle in r.stderr, tag + "_names_cause", r.stderr)
    overlay_untouched(uefi, tag, base or BASE_SHA)


def main():
    # today's real tree shape: a local commit above BASE (the refusal reason)
    # with the port files carried inside it instead of untracked
    tmp = tempfile.mkdtemp(prefix="uefi-export-")
    make_mu(tmp, local_commit=True, with_port=True)
    refused(tmp, "annibale_tree", "but BASE is")
    shutil.rmtree(tmp, ignore_errors=True)

    tmp = tempfile.mkdtemp(prefix="uefi-export-")
    refused(tmp, "missing_mu", "not a git checkout")
    shutil.rmtree(tmp, ignore_errors=True)

    # HEAD at BASE yet without the port files: refuse before rm -rf files
    tmp = tempfile.mkdtemp(prefix="uefi-export-")
    head = make_mu(tmp, local_commit=False, with_port=False)
    refused(tmp, "partial_port", "redwoodPkg", base=head)
    shutil.rmtree(tmp, ignore_errors=True)

    # the pre-preflight script, for the two contrast runs below
    old = old_export_text()
    if old is None:
        print("SKIP old_script_contrast (kernel git HEAD unreachable and XNU_UEFI_EXPORT_OLD unset)")

    # apply.sh shape with the upstream edit staged: git diff would leave it out
    # of the patch, so the guard refuses with the overlay byte-identical
    tmp = tempfile.mkdtemp(prefix="uefi-export-")
    head = make_mu(tmp, local_commit=False, with_port=True)
    stage_bootshim(os.path.join(tmp, "Mu-Silicium"))
    refused(tmp, "staged_edit", "staged", base=head)
    shutil.rmtree(tmp, ignore_errors=True)

    if old is not None:
        tmp = tempfile.mkdtemp(prefix="uefi-export-")
        head = make_mu(tmp, local_commit=False, with_port=True)
        stage_bootshim(os.path.join(tmp, "Mu-Silicium"))
        uefi = make_uefi(tmp)
        set_base(uefi, head)
        r = run_script(uefi, "export-old.sh", old)
        ck(r.returncode == 0, "old_script_staged_ran_anyway", r.stdout + r.stderr)
        ck("BootShim.S" not in read(os.path.join(uefi, "mu-silicium.patch")).decode(),
           "old_script_staged_patch_lost_the_edit")
        ck(not os.path.exists(os.path.join(uefi, "files", "Platforms", "Xiaomi", "sentinel.txt")),
           "old_script_staged_overlay_rebuilt_without_sentinel")
        shutil.rmtree(tmp, ignore_errors=True)

    # redwoodPkg as a regular file: right name, wrong kind
    tmp = tempfile.mkdtemp(prefix="uefi-export-")
    head = make_mu(tmp, local_commit=False, with_port=True)
    break_pkg_dir(os.path.join(tmp, "Mu-Silicium"))
    refused(tmp, "wrong_type", "not a directory", base=head)
    shutil.rmtree(tmp, ignore_errors=True)

    if old is not None:
        tmp = tempfile.mkdtemp(prefix="uefi-export-")
        head = make_mu(tmp, local_commit=False, with_port=True)
        break_pkg_dir(os.path.join(tmp, "Mu-Silicium"))
        uefi = make_uefi(tmp)
        set_base(uefi, head)
        r = run_script(uefi, "export-old.sh", old)
        ck(r.returncode == 0, "old_script_wrongtype_ran_anyway", r.stdout + r.stderr)
        ck(os.path.isfile(os.path.join(uefi, "files", "Platforms", "Xiaomi", "redwoodPkg")),
           "old_script_wrongtype_overlay_shape_broken")
        shutil.rmtree(tmp, ignore_errors=True)

    # apply.sh shape: HEAD at BASE, uncommitted upstream edit, untracked port files
    tmp = tempfile.mkdtemp(prefix="uefi-export-")
    head = make_mu(tmp, local_commit=False, with_port=True)
    uefi = make_uefi(tmp)
    set_base(uefi, head)
    with open(os.path.join(tmp, "Mu-Silicium", "BootShim", "BootShim.S"), "w") as f:
        f.write("patched boot shim\n")
    r = run_export(uefi)
    ck(r.returncode == 0, "apply_shape_exports", r.stdout + r.stderr)
    ck(read(os.path.join(uefi, "BASE")).decode().strip() == head, "apply_shape_BASE_at_HEAD")
    patch = read(os.path.join(uefi, "mu-silicium.patch")).decode()
    ck("BootShim.S" in patch, "apply_shape_patch_has_upstream_edit")
    ck("XnuLoader" not in patch, "apply_shape_patch_clean_of_port_files")
    for path in PORT_FILES:
        ck(os.path.isfile(os.path.join(uefi, "files", path)), "apply_shape_copied_" + path)
    shutil.rmtree(tmp, ignore_errors=True)

    if fails:
        print("\n".join(fails))
        sys.exit(1)
    print("uefi-export: six fixtures pass: refusals keep the fixture overlay byte-identical, "
          "the apply.sh-shaped tree exports%s"
          % ("" if old is None else ", the old-script contrasts reproduce the staged loss and the broken shape"))


if __name__ == "__main__":
    main()
