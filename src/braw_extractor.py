# Copyright 2026 Tagger, LLC -- support@tagger.mov
"""Blackmagic RAW frame extractor -- ctypes COM bridge.

Extracts frames from .braw files via the Blackmagic RAW SDK using its
C++ COM API. Decodes at quarter resolution with Blackmagic Design Video
gamma + Rec. 709 gamut so output is display-ready Rec.709 RGB suitable
for the stitcher and Claude vision API.

Architecture (same on every platform):
  - SDK ships as a C++ shared library (.framework / .dll / .so).
  - We build ctypes vtable wrappers for each required interface.
  - The IBlackmagicRawCallback is implemented as a ctypes COM object
    with a hand-built vtable of CFUNCTYPE function pointers.
  - All jobs are async; threading.Event gates the caller per frame.

Cross-platform notes:
  - macOS: uses system CoreFoundation for CFStringRef.
  - Windows / Linux: the BRAW SDK ships a CFLite shim alongside the main
    library; the CF symbols (CFStringCreateWithCString, CFRelease) are
    re-exported from the main library, so we resolve them out of the
    SDK lib itself rather than the system framework.
  - Calling convention is cdecl on every platform we support; the
    SDK uses the same vtable layout across mac/win/linux.

This port was tested on macOS (BRAW SDK 4.x). Windows + Linux paths are
implemented but not yet validated end-to-end.
"""

from __future__ import annotations

import ctypes
import logging
import os
import platform
import threading
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cross-platform SDK library locations (in priority order)
# ---------------------------------------------------------------------------

def _candidate_lib_paths() -> list[str]:
    system = platform.system()
    if system == "Darwin":
        return [
            "/Applications/Blackmagic RAW/Blackmagic RAW SDK/Mac/Libraries/"
            "BlackmagicRawAPI.framework/BlackmagicRawAPI",
            "/Applications/Blackmagic RAW/Blackmagic RAW Speed Test.app/"
            "Contents/Frameworks/BlackmagicRawAPI.framework/BlackmagicRawAPI",
            "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Frameworks/"
            "BlackmagicRawAPI.framework/BlackmagicRawAPI",
        ]
    if system == "Windows":
        return [
            r"C:\Program Files\Blackmagic Design\Blackmagic RAW\BlackmagicRawAPI.dll",
            r"C:\Program Files\Blackmagic Design\Blackmagic RAW SDK\Win\Libraries\BlackmagicRawAPI.dll",
            r"C:\Program Files\Blackmagic Design\DaVinci Resolve\BlackmagicRawAPI.dll",
        ]
    if system == "Linux":
        return [
            "/usr/lib/blackmagic/BlackmagicRAWSDK/Linux/Libraries/libBlackmagicRawAPI.so",
            "/opt/resolve/libs/libBlackmagicRawAPI.so",
            "/usr/lib/libBlackmagicRawAPI.so",
        ]
    return []


# ---------------------------------------------------------------------------
# CoreFoundation helper (CFStringRef construction).
# On macOS this binds to the system CoreFoundation framework. On Windows
# and Linux the BRAW SDK re-exports the CF symbols from its own library,
# so we attach to the SDK lib itself once it is loaded.
# ---------------------------------------------------------------------------
_kCFStringEncodingUTF8 = 0x08000100
_cf_handle: Optional[ctypes.CDLL] = None


def _bind_corefoundation(lib: ctypes.CDLL) -> ctypes.CDLL:
    """Pick the right binding for CFStringCreateWithCString / CFRelease /
    CFStringGetCString (used to read valid attribute lists back to Python).
    """
    if platform.system() == "Darwin":
        cf = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
    else:
        # Windows + Linux: BRAW SDK re-exports the CF symbols
        cf = lib
    cf.CFStringCreateWithCString.restype  = ctypes.c_void_p
    cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    cf.CFRelease.restype  = None
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    cf.CFStringGetCString.restype  = ctypes.c_bool
    cf.CFStringGetCString.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32,
    ]
    return cf


def _cfstr_to_py(cf_ref) -> Optional[str]:
    """Read a CFStringRef back into a Python str. Returns None on failure."""
    if not cf_ref or _cf_handle is None:
        return None
    buf = ctypes.create_string_buffer(512)
    if _cf_handle.CFStringGetCString(cf_ref, buf, 512, _kCFStringEncodingUTF8):
        return buf.value.decode("utf-8", errors="replace")
    return None


def _cfstr(s: str) -> ctypes.c_void_p:
    """Create a CFStringRef from a Python str. Caller must CFRelease."""
    if _cf_handle is None:
        raise RuntimeError("BRAW SDK not loaded; cannot create CFString")
    return ctypes.c_void_p(
        _cf_handle.CFStringCreateWithCString(None, s.encode("utf-8"), _kCFStringEncodingUTF8)
    )


# ---------------------------------------------------------------------------
# Basic types
# ---------------------------------------------------------------------------
HRESULT = ctypes.c_int32
S_OK    = 0
ULONG   = ctypes.c_ulong


# ---------------------------------------------------------------------------
# Variant struct (from BlackmagicRawAPI.h)
# ---------------------------------------------------------------------------
blackmagicRawVariantTypeString = 7   # CFStringRef value in bstrVal


class _VariantUnion(ctypes.Union):
    _fields_ = [
        ("iVal",    ctypes.c_int16),
        ("uiVal",   ctypes.c_uint16),
        ("intVal",  ctypes.c_int32),
        ("uintVal", ctypes.c_uint32),
        ("fltVal",  ctypes.c_float),
        ("dblVal",  ctypes.c_double),
        ("bstrVal", ctypes.c_void_p),   # CFStringRef
        ("parray",  ctypes.c_void_p),   # SafeArray*
    ]


class Variant(ctypes.Structure):
    _fields_ = [
        ("vt", ctypes.c_uint32),
        ("_u", _VariantUnion),
    ]


def _string_variant(s: str) -> Variant:
    cfref = _cfstr(s)
    v = Variant()
    v.vt = blackmagicRawVariantTypeString
    v._u.bstrVal = cfref.value
    return v


# ---------------------------------------------------------------------------
# SDK constants
# ---------------------------------------------------------------------------
blackmagicRawResourceFormatRGBAU8         = 0x72676261  # 'rgba'
blackmagicRawResolutionScaleQuarter       = 0x71727472  # 'qrtr'
blackmagicRawClipProcessingAttributeGamma = 0x67616D61  # 'gama'
blackmagicRawClipProcessingAttributeGamut = 0x67616D74  # 'gamt'

_TARGET_GAMMA = "Blackmagic Design Video"
_TARGET_GAMUT = "Rec. 709"


# ---------------------------------------------------------------------------
# COM vtable helper
# ---------------------------------------------------------------------------
def _vt_call(this_ptr: int, slot: int, restype, argtypes: list, *args):
    vtable_addr = ctypes.cast(this_ptr, ctypes.POINTER(ctypes.c_void_p))[0]
    fn_addr     = ctypes.cast(vtable_addr, ctypes.POINTER(ctypes.c_void_p))[slot]
    fn_type     = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    fn          = ctypes.cast(fn_addr, fn_type)
    return fn(this_ptr, *args)


def _release(ptr: int):
    if ptr:
        _vt_call(ptr, 2, ULONG, [])


# ---------------------------------------------------------------------------
# IBlackmagicRawFactory wrappers (vtable slots 3-6 after IUnknown)
# ---------------------------------------------------------------------------
def _factory_create_codec(factory: int) -> int:
    codec_out = ctypes.c_void_p(0)
    hr = _vt_call(factory, 3, HRESULT, [ctypes.POINTER(ctypes.c_void_p)],
                  ctypes.byref(codec_out))
    if hr != S_OK:
        raise RuntimeError(f"CreateCodec failed: {hr:#010x}")
    return codec_out.value


# ---------------------------------------------------------------------------
# IBlackmagicRaw wrappers
# ---------------------------------------------------------------------------
def _codec_open_clip(codec: int, path: str) -> int:
    cfpath = _cfstr(path)
    clip_out = ctypes.c_void_p(0)
    hr = _vt_call(codec, 3, HRESULT,
                  [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
                  cfpath, ctypes.byref(clip_out))
    _cf_handle.CFRelease(cfpath)
    if hr != S_OK:
        raise RuntimeError(f"OpenClip failed: {hr:#010x}")
    return clip_out.value


def _codec_set_callback(codec: int, callback_ptr: int):
    hr = _vt_call(codec, 5, HRESULT, [ctypes.c_void_p], callback_ptr)
    if hr != S_OK:
        raise RuntimeError(f"SetCallback failed: {hr:#010x}")


def _codec_flush_jobs(codec: int):
    _vt_call(codec, 8, HRESULT, [])


# ---------------------------------------------------------------------------
# IBlackmagicRawClip wrappers
# ---------------------------------------------------------------------------
def _clip_get_frame_rate(clip: int) -> float:
    val = ctypes.c_float(0)
    _vt_call(clip, 5, HRESULT, [ctypes.POINTER(ctypes.c_float)], ctypes.byref(val))
    return val.value


def _clip_get_frame_count(clip: int) -> int:
    val = ctypes.c_uint64(0)
    _vt_call(clip, 6, HRESULT, [ctypes.POINTER(ctypes.c_uint64)], ctypes.byref(val))
    return val.value


def _clip_clone_clip_processing_attrs(clip: int) -> int:
    out = ctypes.c_void_p(0)
    hr = _vt_call(clip, 12, HRESULT, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(out))
    if hr != S_OK:
        raise RuntimeError(f"CloneClipProcessingAttributes failed: {hr:#010x}")
    return out.value


def _clip_create_job_read_frame(clip: int, frame_index: int) -> int:
    out = ctypes.c_void_p(0)
    hr = _vt_call(clip, 18, HRESULT,
                  [ctypes.c_uint64, ctypes.POINTER(ctypes.c_void_p)],
                  ctypes.c_uint64(frame_index), ctypes.byref(out))
    if hr != S_OK:
        raise RuntimeError(f"CreateJobReadFrame({frame_index}) failed: {hr:#010x}")
    return out.value


def _clip_get_camera_type(clip: int) -> Optional[str]:
    """Read the camera type string for the clip (e.g. 'URSA Mini Pro 12K').

    Wraps IBlackmagicRawClip::GetCameraType (vtable slot 11). Returns None
    if the SDK fails to provide a value.
    """
    out = ctypes.c_void_p(0)
    hr = _vt_call(clip, 11, HRESULT, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(out))
    if hr != S_OK or not out.value:
        return None
    s = _cfstr_to_py(out.value)
    if _cf_handle is not None and out.value:
        _cf_handle.CFRelease(out.value)
    return s


# ---------------------------------------------------------------------------
# IBlackmagicRawClipProcessingAttributes wrappers
# ---------------------------------------------------------------------------
def _attrs_set_clip_attribute(attrs: int, attribute: int, variant: Variant):
    hr = _vt_call(attrs, 4, HRESULT,
                  [ctypes.c_uint32, ctypes.POINTER(Variant)],
                  ctypes.c_uint32(attribute), ctypes.byref(variant))
    return hr == S_OK


def _attrs_get_attribute_list_strings(attrs: int, attribute: int) -> list[str]:
    """Query the list of valid string values for a clip-processing attribute.

    Wraps GetClipAttributeList (vtable slot 6). Two-call protocol:
    1) ask for count with NULL array
    2) allocate array of that count
    3) call again to fill, then read CFStringRef from each Variant.

    Returns [] if the attribute is not a list type or query fails.
    """
    count = ctypes.c_uint32(0)
    is_ro = ctypes.c_bool(False)
    # 1) get required count
    hr = _vt_call(attrs, 6, HRESULT,
                  [ctypes.c_uint32, ctypes.POINTER(Variant),
                   ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_bool)],
                  ctypes.c_uint32(attribute), None,
                  ctypes.byref(count), ctypes.byref(is_ro))
    if hr != S_OK or count.value == 0:
        return []
    # 2) allocate array of Variants and fetch
    arr = (Variant * count.value)()
    hr = _vt_call(attrs, 6, HRESULT,
                  [ctypes.c_uint32, ctypes.POINTER(Variant),
                   ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_bool)],
                  ctypes.c_uint32(attribute), arr,
                  ctypes.byref(count), ctypes.byref(is_ro))
    if hr != S_OK:
        return []
    out: list[str] = []
    for i in range(count.value):
        v = arr[i]
        if v.vt == blackmagicRawVariantTypeString and v._u.bstrVal:
            s = _cfstr_to_py(v._u.bstrVal)
            if s:
                out.append(s)
    return out


# Display-friendly gamma values in priority order. The SDK accepts only
# values that the source codec advertises via GetClipAttributeList; we
# pick the first match from this list. "Blackmagic Design Video" is the
# closest equivalent to a Rec.709 display gamma curve.
_PREFERRED_GAMMA_ORDER = (
    "Blackmagic Design Video",
    "Rec. 709",
    "Rec.709",
    "Rec709",
)

# Display-friendly gamut values in priority order. SDK uses "Blackmagic
# Design" (no "Wide Gamut" suffix). We try Rec.709 spellings first since
# that is the closest match to display target, then fall through to the
# camera-native "Blackmagic Design" gamut which always works.
_PREFERRED_GAMUT_ORDER = (
    "Rec. 709",
    "Rec.709",
    "Rec709",
    "BT.709",
    "Rec. 2020",
    "BT.2020",
    "Blackmagic Design",
)


def _pick_first(supported: list[str], preferences: tuple[str, ...]) -> Optional[str]:
    """Return the first preference present in supported, case-insensitive."""
    if not supported:
        return None
    norm = {s.strip().lower(): s for s in supported}
    for pref in preferences:
        match = norm.get(pref.strip().lower())
        if match:
            return match
    return None


# ---------------------------------------------------------------------------
# IBlackmagicRawFrame wrappers
# ---------------------------------------------------------------------------
def _frame_set_resolution_scale(frame: int, scale: int):
    _vt_call(frame, 9, HRESULT, [ctypes.c_uint32], ctypes.c_uint32(scale))


def _frame_set_resource_format(frame: int, fmt: int):
    _vt_call(frame, 11, HRESULT, [ctypes.c_uint32], ctypes.c_uint32(fmt))


def _frame_create_job_decode_and_process(frame: int, clip_attrs: int, frame_attrs: int) -> int:
    out = ctypes.c_void_p(0)
    hr = _vt_call(frame, 14, HRESULT,
                  [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
                  ctypes.c_void_p(clip_attrs),
                  ctypes.c_void_p(frame_attrs),
                  ctypes.byref(out))
    if hr != S_OK:
        raise RuntimeError(f"CreateJobDecodeAndProcessFrame failed: {hr:#010x}")
    return out.value


# ---------------------------------------------------------------------------
# IBlackmagicRawProcessedImage wrappers
# ---------------------------------------------------------------------------
def _image_get_width(img: int) -> int:
    val = ctypes.c_uint32(0)
    _vt_call(img, 3, HRESULT, [ctypes.POINTER(ctypes.c_uint32)], ctypes.byref(val))
    return val.value


def _image_get_height(img: int) -> int:
    val = ctypes.c_uint32(0)
    _vt_call(img, 4, HRESULT, [ctypes.POINTER(ctypes.c_uint32)], ctypes.byref(val))
    return val.value


def _image_get_resource(img: int) -> int:
    out = ctypes.c_void_p(0)
    _vt_call(img, 5, HRESULT, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(out))
    return out.value


def _image_get_size_bytes(img: int) -> int:
    val = ctypes.c_uint32(0)
    _vt_call(img, 8, HRESULT, [ctypes.POINTER(ctypes.c_uint32)], ctypes.byref(val))
    return val.value


# ---------------------------------------------------------------------------
# IBlackmagicRawJob wrappers
# ---------------------------------------------------------------------------
def _job_submit(job: int):
    hr = _vt_call(job, 3, HRESULT, [])
    if hr != S_OK:
        raise RuntimeError(f"Job.Submit failed: {hr:#010x}")


def _job_set_user_data(job: int, data: int):
    _vt_call(job, 5, HRESULT, [ctypes.c_void_p], ctypes.c_void_p(data))


def _job_get_user_data(job: int) -> int:
    out = ctypes.c_void_p(0)
    _vt_call(job, 6, HRESULT, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(out))
    return out.value or 0


# ---------------------------------------------------------------------------
# IBlackmagicRawCallback -- implemented as a ctypes COM object
# ---------------------------------------------------------------------------
_FT_QI      = ctypes.CFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
_FT_ULONG   = ctypes.CFUNCTYPE(ULONG,   ctypes.c_void_p)
_FT_READ    = ctypes.CFUNCTYPE(None,    ctypes.c_void_p, ctypes.c_void_p, HRESULT, ctypes.c_void_p)
_FT_DECODE  = ctypes.CFUNCTYPE(None,    ctypes.c_void_p, ctypes.c_void_p, HRESULT)
_FT_PROCESS = ctypes.CFUNCTYPE(None,    ctypes.c_void_p, ctypes.c_void_p, HRESULT, ctypes.c_void_p)
_FT_TRIM_P  = ctypes.CFUNCTYPE(None,    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_float)
_FT_TRIM_C  = ctypes.CFUNCTYPE(None,    ctypes.c_void_p, ctypes.c_void_p, HRESULT)
_FT_SIDECAR = ctypes.CFUNCTYPE(None,    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p)
_FT_PREP    = ctypes.CFUNCTYPE(None,    ctypes.c_void_p, ctypes.c_void_p, HRESULT)


class _CallbackVTable(ctypes.Structure):
    _fields_ = [
        ("QueryInterface",              _FT_QI),
        ("AddRef",                      _FT_ULONG),
        ("Release",                     _FT_ULONG),
        ("ReadComplete",                _FT_READ),
        ("DecodeComplete",              _FT_DECODE),
        ("ProcessComplete",             _FT_PROCESS),
        ("TrimProgress",                _FT_TRIM_P),
        ("TrimComplete",                _FT_TRIM_C),
        ("SidecarMetadataParseWarning", _FT_SIDECAR),
        ("SidecarMetadataParseError",   _FT_SIDECAR),
        ("PreparePipelineComplete",     _FT_PREP),
    ]


class _CallbackCOMObject(ctypes.Structure):
    _fields_ = [("vtable_ptr", ctypes.POINTER(_CallbackVTable))]


class _BrawCallbackCOM:
    """Manages the ctypes COM callback object and dispatches to Python handlers.

    One instance per clip extraction. After construction, pass `self.as_ptr()`
    to `_codec_set_callback`.
    """

    def __init__(self, clip_attrs: int):
        self._clip_attrs = clip_attrs
        self._lock    = threading.Lock()
        self._events:  dict = {}   # int -> threading.Event
        self._results: dict = {}   # int -> PIL.Image or None

        # Build vtable; keep refs to prevent GC
        self._qi_fn      = _FT_QI(self._query_interface)
        self._addref_fn  = _FT_ULONG(self._addref)
        self._release_fn = _FT_ULONG(self._release_cb)
        self._read_fn    = _FT_READ(self._read_complete)
        self._decode_fn  = _FT_DECODE(self._decode_complete)
        self._proc_fn    = _FT_PROCESS(self._process_complete)
        self._trim_p_fn  = _FT_TRIM_P(self._trim_progress)
        self._trim_c_fn  = _FT_TRIM_C(self._trim_complete)
        self._side_w_fn  = _FT_SIDECAR(self._sidecar_warning)
        self._side_e_fn  = _FT_SIDECAR(self._sidecar_error)
        self._prep_fn    = _FT_PREP(self._prepare_complete)

        self._vtable = _CallbackVTable(
            QueryInterface              = self._qi_fn,
            AddRef                      = self._addref_fn,
            Release                     = self._release_fn,
            ReadComplete                = self._read_fn,
            DecodeComplete              = self._decode_fn,
            ProcessComplete             = self._proc_fn,
            TrimProgress                = self._trim_p_fn,
            TrimComplete                = self._trim_c_fn,
            SidecarMetadataParseWarning = self._side_w_fn,
            SidecarMetadataParseError   = self._side_e_fn,
            PreparePipelineComplete     = self._prep_fn,
        )
        self._com_obj = _CallbackCOMObject(vtable_ptr=ctypes.pointer(self._vtable))

    def as_ptr(self) -> int:
        return ctypes.addressof(self._com_obj)

    def register_frame(self, frame_index: int) -> threading.Event:
        with self._lock:
            ev = threading.Event()
            self._events[frame_index] = ev
            return ev

    def wait_for_frame(self, frame_index: int, timeout: float = 60.0):
        ev = self._events.get(frame_index)
        if ev is None:
            return None
        if not ev.wait(timeout=timeout):
            logger.error(f"BRAW: timeout waiting for frame {frame_index}")
            return None
        return self._results.get(frame_index)

    # IUnknown stubs
    def _query_interface(self, this, riid, ppv):
        return 0x80004002  # E_NOINTERFACE

    def _addref(self, this):
        return 1

    def _release_cb(self, this):
        return 1

    # IBlackmagicRawCallback implementations
    def _read_complete(self, this, job, result, frame):
        if result != S_OK or not frame:
            idx = _job_get_user_data(job)
            self._deliver(idx, None)
            _release(job)
            return
        try:
            _frame_set_resource_format(frame, blackmagicRawResourceFormatRGBAU8)
            _frame_set_resolution_scale(frame, blackmagicRawResolutionScaleQuarter)
            decode_job = _frame_create_job_decode_and_process(frame, self._clip_attrs, 0)
            idx = _job_get_user_data(job)
            _job_set_user_data(decode_job, idx)
            _job_submit(decode_job)
        except Exception as e:
            logger.error(f"BRAW: ReadComplete error: {e}")
            idx = _job_get_user_data(job)
            self._deliver(idx, None)
        finally:
            _release(job)

    def _decode_complete(self, this, job, result):
        pass

    def _process_complete(self, this, job, result, processed_image):
        import numpy as np
        from PIL import Image as PILImage

        idx = _job_get_user_data(job)
        img = None
        try:
            if result == S_OK and processed_image:
                w   = _image_get_width(processed_image)
                h   = _image_get_height(processed_image)
                sz  = _image_get_size_bytes(processed_image)
                ptr = _image_get_resource(processed_image)
                buf = (ctypes.c_uint8 * sz).from_address(ptr)
                arr = np.frombuffer(buf, dtype=np.uint8).reshape((h, w, 4)).copy()
                img = PILImage.fromarray(arr, mode="RGBA").convert("RGB")
            else:
                logger.warning(f"BRAW: ProcessComplete frame {idx} result={result:#010x}")
        except Exception as e:
            logger.error(f"BRAW: ProcessComplete error on frame {idx}: {e}")
        finally:
            self._deliver(idx, img)
            _release(job)

    def _deliver(self, idx: int, img):
        with self._lock:
            self._results[idx] = img
            ev = self._events.get(idx)
        if ev:
            ev.set()

    def _trim_progress(self, this, job, progress):  pass
    def _trim_complete(self, this, job, result):    pass
    def _sidecar_warning(self, this, clip, fn, ln, info):
        logger.warning(f"BRAW sidecar parse warning at line {ln}")
    def _sidecar_error(self, this, clip, fn, ln, info):
        logger.error(f"BRAW sidecar parse error at line {ln}")
    def _prepare_complete(self, this, ud, result):  pass


# ---------------------------------------------------------------------------
# Module-level factory (loaded once)
# ---------------------------------------------------------------------------
_braw_lib: Optional[ctypes.CDLL] = None
_factory_ptr: int = 0


def _load_sdk() -> bool:
    """Load the BlackmagicRawAPI library and obtain the factory.

    Returns True on success, False if the SDK is not installed. Idempotent.
    """
    global _braw_lib, _factory_ptr, _cf_handle
    if _factory_ptr:
        return True

    candidates = _candidate_lib_paths()
    if not candidates:
        logger.warning(f"BRAW: unsupported platform {platform.system()}")
        return False

    for libpath in candidates:
        if not os.path.exists(libpath):
            continue
        try:
            lib = ctypes.CDLL(libpath)
        except OSError as e:
            logger.debug(f"BRAW: could not load {libpath}: {e}")
            continue

        try:
            _cf = _bind_corefoundation(lib)
        except (AttributeError, OSError) as e:
            logger.debug(f"BRAW: CoreFoundation symbols unavailable in {libpath}: {e}")
            continue

        lib.CreateBlackmagicRawFactoryInstance.restype  = ctypes.c_void_p
        lib.CreateBlackmagicRawFactoryInstance.argtypes = []
        factory = lib.CreateBlackmagicRawFactoryInstance()
        if factory:
            _braw_lib    = lib
            _factory_ptr = factory
            _cf_handle   = _cf
            logger.info(f"BRAW SDK loaded from {libpath}")
            return True
        logger.warning(f"BRAW: CreateBlackmagicRawFactoryInstance returned NULL from {libpath}")

    logger.info(
        "BRAW SDK not installed -- .braw files will be skipped. "
        "Install from https://www.blackmagicdesign.com/developer/product/camera"
    )
    return False


def is_braw_available() -> bool:
    """Return True if the SDK can be loaded on this machine."""
    return _load_sdk()


def _apply_display_color_space(attrs: int, clip_label: str) -> None:
    """Set gamma + gamut to the best display-target match the clip supports.

    BRAW source files come in many camera spaces (Blackmagic Design Film,
    Wide Gamut, etc). The SDK only accepts target gamma/gamut values that
    the codec advertises for this clip. We query GetClipAttributeList for
    each, pick the closest display-friendly match from a priority list,
    and fall back to whatever the SDK already had set if no preferred
    value is offered (the cloned attrs default to a sensible camera native).
    """
    # Gamma
    supported_gamma = _attrs_get_attribute_list_strings(
        attrs, blackmagicRawClipProcessingAttributeGamma
    )
    chosen_gamma = _pick_first(supported_gamma, _PREFERRED_GAMMA_ORDER)
    if chosen_gamma:
        v = _string_variant(chosen_gamma)
        if _attrs_set_clip_attribute(attrs, blackmagicRawClipProcessingAttributeGamma, v):
            logger.info(f"BRAW {clip_label}: gamma -> {chosen_gamma}")
        else:
            logger.warning(f"BRAW {clip_label}: SDK rejected gamma {chosen_gamma!r}")
    elif supported_gamma:
        logger.info(
            f"BRAW {clip_label}: keeping native gamma "
            f"(SDK offers {supported_gamma}, none preferred)"
        )

    # Gamut
    supported_gamut = _attrs_get_attribute_list_strings(
        attrs, blackmagicRawClipProcessingAttributeGamut
    )
    chosen_gamut = _pick_first(supported_gamut, _PREFERRED_GAMUT_ORDER)
    if chosen_gamut:
        v = _string_variant(chosen_gamut)
        if _attrs_set_clip_attribute(attrs, blackmagicRawClipProcessingAttributeGamut, v):
            logger.info(f"BRAW {clip_label}: gamut -> {chosen_gamut}")
        else:
            logger.warning(f"BRAW {clip_label}: SDK rejected gamut {chosen_gamut!r}")
    elif supported_gamut:
        logger.info(
            f"BRAW {clip_label}: keeping native gamut "
            f"(SDK offers {supported_gamut}, none preferred)"
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def extract_frames_braw(
    video_path: str,
    percentages: List[float],
) -> Tuple[list, None]:
    """Extract frames from a .braw file at percentage positions.

    Decodes at quarter resolution with Blackmagic Design Video gamma + Rec.709
    gamut so output is display-ready RGB ready for the stitcher.

    Args:
        video_path:   Absolute path to a .braw file.
        percentages:  List of floats in [0.0, 1.0].

    Returns:
        (list of PIL.Image, None). Returns ([], None) on failure.
    """
    if not _load_sdk():
        return [], None

    if not os.path.exists(video_path):
        logger.error(f"BRAW: file not found: {video_path}")
        return [], None

    codec = clip = attrs = 0
    try:
        codec = _factory_create_codec(_factory_ptr)
        clip  = _codec_open_clip(codec, video_path)

        frame_count = _clip_get_frame_count(clip)
        frame_rate  = _clip_get_frame_rate(clip)
        if frame_count <= 0 or frame_rate <= 0:
            raise RuntimeError(f"Invalid clip properties: count={frame_count} fps={frame_rate}")

        logger.info(
            f"BRAW: opened {Path(video_path).name} "
            f"({frame_count} frames @ {frame_rate:.3f} fps)"
        )

        attrs = _clip_clone_clip_processing_attrs(clip)
        _apply_display_color_space(attrs, Path(video_path).name)

        cb = _BrawCallbackCOM(attrs)
        _codec_set_callback(codec, cb.as_ptr())

        target_indices = [
            min(int(pct * frame_count), frame_count - 1)
            for pct in percentages
        ]
        for idx in target_indices:
            cb.register_frame(idx)
        for idx in target_indices:
            job = _clip_create_job_read_frame(clip, idx)
            _job_set_user_data(job, idx)
            _job_submit(job)

        _codec_flush_jobs(codec)

        images = []
        for idx in target_indices:
            img = cb.wait_for_frame(idx, timeout=5.0)
            if img is not None:
                images.append(img)
            else:
                logger.warning(f"BRAW: frame {idx} not delivered")

        logger.info(
            f"BRAW extracted {len(images)}/{len(percentages)} frames "
            f"from {Path(video_path).name}"
        )
        return images, None

    except Exception as e:
        logger.error(f"BRAW extraction failed: {e}")
        return [], None
    finally:
        if attrs: _release(attrs)
        if clip:  _release(clip)
        if codec: _release(codec)


def get_braw_duration(video_path: str) -> Optional[float]:
    """Return duration in seconds for a .braw file, or None on failure."""
    if not _load_sdk():
        return None
    if not os.path.exists(video_path):
        return None
    codec = clip = 0
    try:
        codec = _factory_create_codec(_factory_ptr)
        clip  = _codec_open_clip(codec, video_path)
        frames = _clip_get_frame_count(clip)
        fps    = _clip_get_frame_rate(clip)
        if frames <= 0 or fps <= 0:
            return None
        return float(frames) / float(fps)
    except Exception as e:
        logger.error(f"BRAW duration failed for {video_path}: {e}")
        return None
    finally:
        if clip:  _release(clip)
        if codec: _release(codec)


def get_braw_info(video_path: str) -> dict:
    """Return {codec, width, height, fps, camera_make, camera_model} for a .braw.

    Width/height come from a single decoded frame at quarter resolution; we
    multiply by 4 to recover the source resolution. (BRAW does not expose
    a direct source-resolution property in the SDK.) BRAW is BMD-exclusive
    so camera_make is always 'Blackmagic'; camera_model comes from the SDK
    via IBlackmagicRawClip::GetCameraType.
    """
    if not _load_sdk():
        return {}
    if not os.path.exists(video_path):
        return {}
    codec = clip = attrs = 0
    try:
        codec = _factory_create_codec(_factory_ptr)
        clip  = _codec_open_clip(codec, video_path)
        fps   = _clip_get_frame_rate(clip)
        camera_model = _clip_get_camera_type(clip)
        attrs = _clip_clone_clip_processing_attrs(clip)
        cb    = _BrawCallbackCOM(attrs)
        _codec_set_callback(codec, cb.as_ptr())
        cb.register_frame(0)
        job = _clip_create_job_read_frame(clip, 0)
        _job_set_user_data(job, 0)
        _job_submit(job)
        _codec_flush_jobs(codec)
        img = cb.wait_for_frame(0, timeout=10.0)
        # Strip redundant 'Blackmagic ' prefix from model since make is
        # captured separately. e.g. 'Blackmagic Pocket Cinema Camera 4K'
        # -> 'Pocket Cinema Camera 4K' for cleaner Resolve bin labels.
        cleaned_model = camera_model
        if cleaned_model and cleaned_model.lower().startswith("blackmagic "):
            cleaned_model = cleaned_model[len("blackmagic "):].lstrip()
        info = {
            "codec":        "BRAW",
            "fps":          fps,
            "camera_make":  "Blackmagic",
            "camera_model": cleaned_model or "Unknown",
        }
        if img is None:
            info["width"]  = 0
            info["height"] = 0
        else:
            # Quarter resolution -> multiply by 4 to recover source dims
            info["width"]  = img.width * 4
            info["height"] = img.height * 4
        return info
    except Exception as e:
        logger.error(f"BRAW info failed for {video_path}: {e}")
        return {}
    finally:
        if attrs: _release(attrs)
        if clip:  _release(clip)
        if codec: _release(codec)
