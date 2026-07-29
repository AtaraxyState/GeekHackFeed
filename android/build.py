#!/usr/bin/env python3
"""Build geekhack-feed.apk straight from the Android SDK tools.

No Gradle, no Android Studio, nothing downloaded. Runs on Windows (where it
finds the SDK and JDK that ship with Unity) and on Linux (where it uses
ANDROID_SDK_ROOT), so local builds and CI go through the same code.

    python build.py
    python build.py --version-name 1.2.0 --version-code 10200
    python build.py --keystore release.jks --ks-pass env:KS_PASS
    python build.py --install
"""

from __future__ import annotations

import argparse
import glob
import os
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
PACKAGE = "com.geekhackfeed"
MIN_SDK = 24
TARGET_SDK = 34
WINDOWS = os.name == "nt"


def fail(message):
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def step(message):
    print(f"\n==> {message}", flush=True)


def run(cmd, **kwargs):
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        fail(f"{os.path.basename(str(cmd[0]))} failed ({result.returncode})")


def tool(directory, name, windows_ext):
    """SDK tools are bare names on Linux and .exe/.bat on Windows."""
    path = os.path.join(directory, name + (windows_ext if WINDOWS else ""))
    return path if os.path.exists(path) else None


# --------------------------------------------------------------------------
# toolchain discovery
# --------------------------------------------------------------------------


def find_sdk(explicit):
    if explicit:
        return explicit
    for var in ("ANDROID_SDK_ROOT", "ANDROID_HOME"):
        value = os.environ.get(var)
        if value and os.path.isdir(value):
            return value
    if WINDOWS:
        # Unity ships a complete SDK, which is usually the only one present.
        for root in (r"C:\Program Files\Unity\Hub\Editor",):
            if not os.path.isdir(root):
                continue
            for version in sorted(os.listdir(root), reverse=True):
                candidate = os.path.join(
                    root, version, "Editor", "Data", "PlaybackEngines",
                    "AndroidPlayer", "SDK",
                )
                if os.path.isdir(os.path.join(candidate, "platform-tools")):
                    return candidate
    return None


def find_jdk(explicit, sdk):
    if explicit:
        return explicit
    java_home = os.environ.get("JAVA_HOME")
    if java_home and os.path.isdir(java_home):
        return java_home
    if sdk:
        unity = os.path.join(os.path.dirname(sdk), "OpenJDK")
        if os.path.isdir(unity):
            return unity
    javac = shutil.which("javac")
    if javac:
        return os.path.dirname(os.path.dirname(javac))
    return None


def newest_build_tools(sdk):
    root = os.path.join(sdk, "build-tools")
    if not os.path.isdir(root):
        return None

    def version_key(name):
        parts = []
        for chunk in name.split("."):
            parts.append(int(chunk) if chunk.isdigit() else 0)
        return parts

    versions = sorted(os.listdir(root), key=version_key, reverse=True)
    return os.path.join(root, versions[0]) if versions else None


def pick_platform(sdk):
    """Prefer the platform we target, then anything newer that exists."""
    for name in (f"android-{TARGET_SDK}", "android-35", "android-36", "android-33"):
        jar = os.path.join(sdk, "platforms", name, "android.jar")
        if os.path.exists(jar):
            return jar
    found = sorted(glob.glob(os.path.join(sdk, "platforms", "android-*", "android.jar")))
    return found[-1] if found else None


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------


def make_icons():
    try:
        import PIL  # noqa: F401
    except ImportError:
        if not glob.glob(os.path.join(HERE, "res", "mipmap-*", "ic_launcher.png")):
            fail("Pillow is needed to generate launcher icons (pip install pillow)")
        print("  Pillow missing - reusing the icons already in res/")
        return
    sys.path.insert(0, HERE)
    import make_icon

    make_icon.main()


def build(args):
    sdk = find_sdk(args.sdk)
    if not sdk:
        fail("Android SDK not found. Set ANDROID_SDK_ROOT or pass --sdk.")
    jdk = find_jdk(args.jdk, sdk)
    if not jdk:
        fail("JDK not found. Set JAVA_HOME or pass --jdk.")

    build_tools = newest_build_tools(sdk)
    if not build_tools:
        fail(f"no build-tools under {sdk}")
    platform = pick_platform(sdk)
    if not platform:
        fail(f"no android.jar under {sdk}/platforms")

    aapt2 = tool(build_tools, "aapt2", ".exe")
    zipalign = tool(build_tools, "zipalign", ".exe")
    apksigner = tool(build_tools, "apksigner", ".bat")
    d8 = tool(build_tools, "d8", ".bat")
    javac = tool(os.path.join(jdk, "bin"), "javac", ".exe")
    keytool = tool(os.path.join(jdk, "bin"), "keytool", ".exe")

    for name, path in [("aapt2", aapt2), ("zipalign", zipalign),
                       ("apksigner", apksigner), ("d8", d8), ("javac", javac)]:
        if not path:
            fail(f"missing tool: {name} (looked in {build_tools} / {jdk})")

    # d8 and apksigner are wrappers that shell out to java.
    env_java = dict(os.environ, JAVA_HOME=jdk)
    env_java["PATH"] = os.path.join(jdk, "bin") + os.pathsep + env_java.get("PATH", "")

    print(f"SDK         {sdk}")
    print(f"build-tools {os.path.basename(build_tools)}")
    print(f"platform    {os.path.basename(os.path.dirname(platform))}")
    print(f"JDK         {jdk}")
    print(f"version     {args.version_name} (code {args.version_code})")

    out = os.path.join(HERE, "build")
    if os.path.isdir(out):
        shutil.rmtree(out)
    os.makedirs(out)

    step("launcher icons")
    make_icons()

    step("compiling resources")
    res_zip = os.path.join(out, "res.zip")
    run([aapt2, "compile", "--dir", os.path.join(HERE, "res"), "-o", res_zip])

    step("linking resources")
    base_apk = os.path.join(out, "base.apk")
    gen = os.path.join(out, "gen")
    os.makedirs(gen)
    run([
        aapt2, "link", "-o", base_apk, "-I", platform,
        "--manifest", os.path.join(HERE, "AndroidManifest.xml"),
        "-R", res_zip, "--java", gen,
        "--min-sdk-version", str(MIN_SDK),
        "--target-sdk-version", str(TARGET_SDK),
        "--version-code", str(args.version_code),
        "--version-name", args.version_name,
        "--auto-add-overlay",
    ])

    step("compiling java")
    classes = os.path.join(out, "classes")
    os.makedirs(classes)
    sources = glob.glob(os.path.join(HERE, "src", "**", "*.java"), recursive=True)
    sources += glob.glob(os.path.join(gen, "**", "R.java"), recursive=True)
    run([javac, "--release", "11", "-nowarn", "-classpath", platform,
         "-d", classes] + sources)

    step("dexing")
    dex_dir = os.path.join(out, "dex")
    os.makedirs(dex_dir)
    class_files = glob.glob(os.path.join(classes, "**", "*.class"), recursive=True)
    run([d8, "--lib", platform, "--min-api", str(MIN_SDK),
         "--output", dex_dir] + class_files, env=env_java)

    step("packaging")
    # zipfile rather than the legacy `aapt add`, which newer build-tools drop.
    with zipfile.ZipFile(base_apk, "a", zipfile.ZIP_DEFLATED) as apk:
        apk.write(os.path.join(dex_dir, "classes.dex"), "classes.dex")

    step("aligning")
    aligned = os.path.join(out, "aligned.apk")
    run([zipalign, "-f", "4", base_apk, aligned])

    step("signing")
    keystore = args.keystore or os.path.join(HERE, "debug.keystore")
    ks_pass = args.ks_pass or "pass:android"
    key_pass = args.key_pass or ks_pass
    alias = args.key_alias or "androiddebugkey"

    if not os.path.exists(keystore):
        if args.keystore:
            fail(f"keystore not found: {keystore}")
        if not keytool:
            fail("keytool not found and no keystore present")
        print("  creating a local debug keystore")
        run([keytool, "-genkeypair", "-keystore", keystore,
             "-storepass", "android", "-keypass", "android",
             "-alias", "androiddebugkey", "-keyalg", "RSA", "-keysize", "2048",
             "-validity", "10000", "-dname", "CN=geekhack feed"], env=env_java)

    apk_path = args.out or os.path.join(HERE, "geekhack-feed.apk")
    run([apksigner, "sign", "--ks", keystore, "--ks-pass", ks_pass,
         "--key-pass", key_pass, "--ks-key-alias", alias,
         "--out", apk_path, aligned], env=env_java)
    run([apksigner, "verify", apk_path], env=env_java)

    size = os.path.getsize(apk_path) / 1024
    print(f"\nBuilt {apk_path} ({size:,.0f} KB)")

    if args.install:
        step("installing")
        adb = tool(os.path.join(sdk, "platform-tools"), "adb", ".exe")
        if not adb:
            fail("adb not found")
        run([adb, "install", "-r", apk_path])

    return apk_path


def semver_to_code(version):
    """1.2.3 -> 10203, so a newer tag always sorts above an older one."""
    parts = (version.lstrip("vV").split("-")[0].split(".") + ["0", "0"])[:3]
    try:
        major, minor, patch = (int(p) for p in parts)
    except ValueError:
        return 1
    return major * 10000 + minor * 100 + patch


def main():
    parser = argparse.ArgumentParser(description="Build geekhack-feed.apk")
    parser.add_argument("--sdk")
    parser.add_argument("--jdk")
    parser.add_argument("--version-name", default="1.0.0")
    parser.add_argument("--version-code", type=int)
    parser.add_argument("--keystore")
    parser.add_argument("--ks-pass", help="apksigner form, e.g. pass:x or env:VAR")
    parser.add_argument("--key-pass")
    parser.add_argument("--key-alias")
    parser.add_argument("--out")
    parser.add_argument("--install", action="store_true")
    args = parser.parse_args()

    args.version_name = args.version_name.lstrip("vV")
    if args.version_code is None:
        args.version_code = semver_to_code(args.version_name)

    build(args)


if __name__ == "__main__":
    main()
