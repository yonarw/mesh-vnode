"""mesh_vnode - a virtual node with a message store, for use with Meshtastic meshes."""

from importlib.metadata import PackageNotFoundError, version

# The version lives in exactly one hand-edited place: `version` in
# pyproject.toml. Everything else - the image tag, the add-on manifest, the git
# tag, what the web UI shows - is derived from it by scripts/bump-version.sh and
# checked by CI. Reading it back from the installed metadata keeps this file
# from becoming a second copy that drifts.
try:
    __version__ = version("mesh-vnode")
except PackageNotFoundError:  # a source tree that was never installed
    __version__ = "0+unknown"
