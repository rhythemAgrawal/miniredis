/*
 * miniredis._rdb - RDB-format snapshot encoder and decoder.
 *
 * Exposes two functions to Python:
 *   _rdb.dump(data: dict, ttl: dict, path: str) -> None
 *   _rdb.load(path: str) -> tuple[dict, dict]
 *
 * Format implemented is a strict subset of Redis RDB v11:
 *   - Magic "REDIS" + version "0011".
 *   - One SELECTDB 0 section.
 *   - Per-key: optional EXPIRETIME_MS opcode + 8-byte LE unix-ms timestamp,
 *     then a type byte (STRING=0x00, LIST=0x01, HASH=0x04, ZSET_2=0x05),
 *     then the key (length-prefixed bytes), then the value.
 *   - EOF opcode (0xFF) and 8-byte trailer (zeroed: signals "checksum
 *     disabled" to Redis's loader). A real CRC64-Jones can drop in later
 *     without changing any callers.
 *
 * Length encoding is RDB's variable-length scheme:
 *   00xxxxxx           -> 1 byte  (value 0..63)
 *   01xxxxxx xxxxxxxx  -> 2 bytes (value 0..16383, big-endian 14-bit)
 *   10000000 + 4 bytes -> 5 bytes (big-endian uint32)
 *   10000001 + 8 bytes -> 9 bytes (big-endian uint64)
 *
 * On dump, the dict walk uses PyDict_Next (borrowed references) so refcounts
 * of stored objects are not bumped during traversal -- which is the whole
 * point of doing this in C: it preserves CoW page sharing after fork().
 *
 * Build: see setup.py.  Target: POSIX (uses fsync/unlink/rename).
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>


/* ---------- Cached type pointers, populated at module init ------------- */

static PyObject *cached_deque_type = NULL;
static PyObject *cached_sorted_set_type = NULL;


/* ---------- RDB opcodes and type bytes --------------------------------- */

#define RDB_OPCODE_SELECTDB      0xFE
#define RDB_OPCODE_EXPIRETIME_MS 0xFC
#define RDB_OPCODE_EOF           0xFF

#define RDB_TYPE_STRING 0x00
#define RDB_TYPE_LIST   0x01
#define RDB_TYPE_HASH   0x04
#define RDB_TYPE_ZSET_2 0x05

static const char RDB_MAGIC[5]   = {'R', 'E', 'D', 'I', 'S'};
static const char RDB_VERSION[4] = {'0', '0', '1', '1'};


/* =======================================================================
 * Low-level write helpers (encoder).
 * Return 0 on success, -1 on failure with a Python exception set.
 * ======================================================================= */

static int
write_bytes(FILE *fp, const void *buf, size_t n)
{
    if (n == 0) {
        return 0;
    }
    if (fwrite(buf, 1, n, fp) != n) {
        PyErr_SetFromErrno(PyExc_OSError);
        return -1;
    }
    return 0;
}

static int
write_byte(FILE *fp, uint8_t b)
{
    return write_bytes(fp, &b, 1);
}

static int
write_length(FILE *fp, uint64_t len)
{
    uint8_t buf[9];

    if (len < 64) {
        buf[0] = (uint8_t)len;                       /* 00xxxxxx */
        return write_bytes(fp, buf, 1);
    } else if (len < 16384) {
        buf[0] = 0x40 | (uint8_t)((len >> 8) & 0x3F); /* 01xxxxxx */
        buf[1] = (uint8_t)(len & 0xFF);
        return write_bytes(fp, buf, 2);
    } else if (len <= 0xFFFFFFFFULL) {
        buf[0] = 0x80;
        buf[1] = (uint8_t)((len >> 24) & 0xFF);
        buf[2] = (uint8_t)((len >> 16) & 0xFF);
        buf[3] = (uint8_t)((len >>  8) & 0xFF);
        buf[4] = (uint8_t)( len        & 0xFF);
        return write_bytes(fp, buf, 5);
    } else {
        buf[0] = 0x81;
        for (int i = 0; i < 8; i++) {
            buf[1 + i] = (uint8_t)((len >> (56 - 8 * i)) & 0xFF);
        }
        return write_bytes(fp, buf, 9);
    }
}

/* Write a length-prefixed bytestring. `obj` is a borrowed reference. */
static int
write_string_object(FILE *fp, PyObject *obj)
{
    char *buf;
    Py_ssize_t len;
    if (PyBytes_AsStringAndSize(obj, &buf, &len) < 0) {
        return -1;
    }
    if (write_length(fp, (uint64_t)len) < 0) {
        return -1;
    }
    return write_bytes(fp, buf, (size_t)len);
}

static int
write_double_le(FILE *fp, double d)
{
    union { double d; uint64_t u; } conv;
    conv.d = d;
    uint8_t buf[8];
    for (int i = 0; i < 8; i++) {
        buf[i] = (uint8_t)((conv.u >> (8 * i)) & 0xFF);
    }
    return write_bytes(fp, buf, 8);
}

static int
write_uint64_le(FILE *fp, uint64_t v)
{
    uint8_t buf[8];
    for (int i = 0; i < 8; i++) {
        buf[i] = (uint8_t)((v >> (8 * i)) & 0xFF);
    }
    return write_bytes(fp, buf, 8);
}


/* =======================================================================
 * Type-specific value encoders.
 * Each takes a borrowed reference to the Python value object.
 * ======================================================================= */

static int
encode_string(FILE *fp, PyObject *value)
{
    if (!PyBytes_CheckExact(value)) {
        PyErr_SetString(PyExc_TypeError, "expected bytes value");
        return -1;
    }
    return write_string_object(fp, value);
}

static int
encode_list(FILE *fp, PyObject *value)
{
    /*
     * collections.deque has no public borrowed-reference iteration API.
     * PyObject_GetIter / PyIter_Next return NEW references per element --
     * a bounded refcount cost per list element. The main dict walk in
     * rdb_dump() remains borrowed-ref via PyDict_Next, which is where
     * the CoW preservation matters.
     */
    Py_ssize_t length = PyObject_Length(value);
    if (length < 0) {
        return -1;
    }
    if (write_length(fp, (uint64_t)length) < 0) {
        return -1;
    }

    PyObject *iter = PyObject_GetIter(value);
    if (iter == NULL) {
        return -1;
    }

    PyObject *item;
    while ((item = PyIter_Next(iter)) != NULL) {
        int rc = write_string_object(fp, item);
        Py_DECREF(item);
        if (rc < 0) {
            Py_DECREF(iter);
            return -1;
        }
    }
    Py_DECREF(iter);
    return PyErr_Occurred() ? -1 : 0;
}

static int
encode_hash(FILE *fp, PyObject *value)
{
    if (write_length(fp, (uint64_t)PyDict_Size(value)) < 0) {
        return -1;
    }

    PyObject *field, *val;
    Py_ssize_t pos = 0;
    while (PyDict_Next(value, &pos, &field, &val)) {
        if (write_string_object(fp, field) < 0) return -1;
        if (write_string_object(fp, val)   < 0) return -1;
    }
    return 0;
}

static int
encode_zset(FILE *fp, PyObject *value)
{
    /*
     * SortedSet stores members in its `score_table` dict (member -> score).
     * The skip list is redundant for serialization: at load time we just
     * call SortedSet.insert(score, member) per pair and the skip list is
     * rebuilt from scratch.
     */
    PyObject *score_table = PyObject_GetAttrString(value, "score_table");
    if (score_table == NULL) {
        return -1;
    }
    if (!PyDict_CheckExact(score_table)) {
        Py_DECREF(score_table);
        PyErr_SetString(PyExc_TypeError,
                        "SortedSet.score_table is not a dict");
        return -1;
    }

    int rc = 0;
    if (write_length(fp, (uint64_t)PyDict_Size(score_table)) < 0) {
        rc = -1;
        goto done;
    }

    PyObject *member, *score_obj;
    Py_ssize_t pos = 0;
    while (PyDict_Next(score_table, &pos, &member, &score_obj)) {
        if (write_string_object(fp, member) < 0) { rc = -1; goto done; }
        double score = PyFloat_AsDouble(score_obj);
        if (score == -1.0 && PyErr_Occurred()) { rc = -1; goto done; }
        if (write_double_le(fp, score) < 0) { rc = -1; goto done; }
    }

done:
    Py_DECREF(score_table);
    return rc;
}

static int
type_byte_for_value(PyObject *value, uint8_t *out)
{
    if (PyBytes_CheckExact(value)) {
        *out = RDB_TYPE_STRING;
        return 0;
    }
    if (PyDict_CheckExact(value)) {
        *out = RDB_TYPE_HASH;
        return 0;
    }
    if ((PyObject *)Py_TYPE(value) == cached_deque_type) {
        *out = RDB_TYPE_LIST;
        return 0;
    }
    if ((PyObject *)Py_TYPE(value) == cached_sorted_set_type) {
        *out = RDB_TYPE_ZSET_2;
        return 0;
    }
    PyErr_Format(PyExc_TypeError,
                 "unsupported value type for RDB encoding: %s",
                 Py_TYPE(value)->tp_name);
    return -1;
}


/* =======================================================================
 * rdb_dump(data, ttl, path) -> None
 * ======================================================================= */

static PyObject *
rdb_dump(PyObject *self, PyObject *args)
{
    PyObject *data;     /* dict[bytes, value]                        */
    PyObject *ttl;      /* dict[bytes, float] (absolute unix seconds)*/
    const char *path;

    if (!PyArg_ParseTuple(args, "O!O!s",
                          &PyDict_Type, &data,
                          &PyDict_Type, &ttl,
                          &path)) {
        return NULL;
    }

    /* Atomic-write pattern: open <path>.tmp, write, fsync, rename. */
    size_t plen = strlen(path);
    char *tmp_path = (char *)PyMem_Malloc(plen + 5);  /* + ".tmp\0" */
    if (tmp_path == NULL) {
        PyErr_NoMemory();
        return NULL;
    }
    memcpy(tmp_path, path, plen);
    memcpy(tmp_path + plen, ".tmp", 5);  /* trailing NUL included */

    FILE *fp = fopen(tmp_path, "wb");
    if (fp == NULL) {
        PyErr_SetFromErrnoWithFilename(PyExc_OSError, tmp_path);
        PyMem_Free(tmp_path);
        return NULL;
    }

    /* Header: REDIS0011. */
    if (write_bytes(fp, RDB_MAGIC,   5) < 0) goto fail;
    if (write_bytes(fp, RDB_VERSION, 4) < 0) goto fail;

    /* SELECTDB 0. */
    if (write_byte(fp, RDB_OPCODE_SELECTDB) < 0) goto fail;
    if (write_length(fp, 0) < 0) goto fail;

    /* Walk the top-level data dict with borrowed references. */
    PyObject *key, *value;
    Py_ssize_t pos = 0;
    while (PyDict_Next(data, &pos, &key, &value)) {
        /* Optional TTL precedes the type byte. */
        PyObject *ttl_obj = PyDict_GetItem(ttl, key);  /* borrowed, may be NULL */
        if (ttl_obj != NULL) {
            double ttl_seconds = PyFloat_AsDouble(ttl_obj);
            if (ttl_seconds == -1.0 && PyErr_Occurred()) goto fail;
            uint64_t ttl_ms = (uint64_t)(ttl_seconds * 1000.0);
            if (write_byte(fp, RDB_OPCODE_EXPIRETIME_MS) < 0) goto fail;
            if (write_uint64_le(fp, ttl_ms) < 0) goto fail;
        }

        /* Type byte + key + value. */
        uint8_t type_byte;
        if (type_byte_for_value(value, &type_byte) < 0) goto fail;
        if (write_byte(fp, type_byte) < 0) goto fail;
        if (write_string_object(fp, key) < 0) goto fail;

        int rc = 0;
        switch (type_byte) {
            case RDB_TYPE_STRING: rc = encode_string(fp, value); break;
            case RDB_TYPE_LIST:   rc = encode_list(fp, value);   break;
            case RDB_TYPE_HASH:   rc = encode_hash(fp, value);   break;
            case RDB_TYPE_ZSET_2: rc = encode_zset(fp, value);   break;
        }
        if (rc < 0) goto fail;
    }

    /* EOF opcode + 8-byte trailer (zeroed: tells Redis's loader to skip
     * CRC verification). A real CRC64-Jones can replace these 8 bytes. */
    if (write_byte(fp, RDB_OPCODE_EOF) < 0) goto fail;
    uint8_t zero8[8] = {0};
    if (write_bytes(fp, zero8, 8) < 0) goto fail;

    /* Flush stdio buffers, then fsync to push the page cache out. */
    if (fflush(fp) != 0) {
        PyErr_SetFromErrno(PyExc_OSError);
        goto fail;
    }
    int fd = fileno(fp);
    int sync_rc = 0;
    if (fd >= 0) {
        /* Release the GIL across the blocking fsync syscall. */
        Py_BEGIN_ALLOW_THREADS
        sync_rc = fsync(fd);
        Py_END_ALLOW_THREADS
    }
    if (sync_rc != 0) {
        PyErr_SetFromErrno(PyExc_OSError);
        goto fail;
    }
    fclose(fp);
    fp = NULL;

    /* Atomic rename. The directory itself isn't fsynced here; for full
     * crash durability of the rename, the caller can fsync the parent
     * directory after this call returns. */
    if (rename(tmp_path, path) != 0) {
        PyErr_SetFromErrnoWithFilename(PyExc_OSError, path);
        goto fail;
    }

    PyMem_Free(tmp_path);
    Py_RETURN_NONE;

fail:
    if (fp != NULL) fclose(fp);
    unlink(tmp_path);
    PyMem_Free(tmp_path);
    return NULL;
}


/* =======================================================================
 * Low-level read helpers (decoder).
 * ======================================================================= */

static int
read_bytes(FILE *fp, void *buf, size_t n)
{
    if (n == 0) {
        return 0;
    }
    if (fread(buf, 1, n, fp) != n) {
        if (feof(fp)) {
            PyErr_SetString(PyExc_ValueError,
                            "unexpected EOF in RDB file");
        } else {
            PyErr_SetFromErrno(PyExc_OSError);
        }
        return -1;
    }
    return 0;
}

static int
read_byte(FILE *fp, uint8_t *out)
{
    return read_bytes(fp, out, 1);
}

static int
read_length(FILE *fp, uint64_t *out)
{
    uint8_t first;
    if (read_byte(fp, &first) < 0) return -1;
    uint8_t top2 = first >> 6;

    if (top2 == 0) {
        *out = first & 0x3F;
        return 0;
    } else if (top2 == 1) {
        uint8_t second;
        if (read_byte(fp, &second) < 0) return -1;
        *out = ((uint64_t)(first & 0x3F) << 8) | (uint64_t)second;
        return 0;
    } else if (first == 0x80) {
        uint8_t buf[4];
        if (read_bytes(fp, buf, 4) < 0) return -1;
        *out = ((uint64_t)buf[0] << 24) | ((uint64_t)buf[1] << 16)
             | ((uint64_t)buf[2] <<  8) |  (uint64_t)buf[3];
        return 0;
    } else if (first == 0x81) {
        uint8_t buf[8];
        if (read_bytes(fp, buf, 8) < 0) return -1;
        *out = 0;
        for (int i = 0; i < 8; i++) {
            *out = (*out << 8) | (uint64_t)buf[i];
        }
        return 0;
    } else {
        /* Special-encoding length prefixes (11xxxxxx) are not implemented
         * by this writer, so we don't accept them on read either. Real
         * Redis dumps would trip this; that's deliberate scope-limiting. */
        PyErr_Format(PyExc_ValueError,
                     "unsupported RDB length prefix 0x%02X", first);
        return -1;
    }
}

/* Returns a NEW reference. */
static PyObject *
read_string_object(FILE *fp)
{
    uint64_t length;
    if (read_length(fp, &length) < 0) return NULL;

    PyObject *result = PyBytes_FromStringAndSize(NULL, (Py_ssize_t)length);
    if (result == NULL) return NULL;
    if (length > 0) {
        char *buf = PyBytes_AS_STRING(result);
        if (read_bytes(fp, buf, (size_t)length) < 0) {
            Py_DECREF(result);
            return NULL;
        }
    }
    return result;
}

static int
read_double_le(FILE *fp, double *out)
{
    uint8_t buf[8];
    if (read_bytes(fp, buf, 8) < 0) return -1;
    union { uint64_t u; double d; } conv;
    conv.u = 0;
    for (int i = 7; i >= 0; i--) {
        conv.u = (conv.u << 8) | (uint64_t)buf[i];
    }
    *out = conv.d;
    return 0;
}

static int
read_uint64_le(FILE *fp, uint64_t *out)
{
    uint8_t buf[8];
    if (read_bytes(fp, buf, 8) < 0) return -1;
    *out = 0;
    for (int i = 7; i >= 0; i--) {
        *out = (*out << 8) | (uint64_t)buf[i];
    }
    return 0;
}


/* =======================================================================
 * Type-specific value decoders. Return a NEW reference or NULL on error.
 * ======================================================================= */

static PyObject *
decode_string(FILE *fp)
{
    return read_string_object(fp);
}

static PyObject *
decode_list(FILE *fp)
{
    uint64_t count;
    if (read_length(fp, &count) < 0) return NULL;

    PyObject *deque = PyObject_CallNoArgs(cached_deque_type);
    if (deque == NULL) return NULL;

    PyObject *append = PyObject_GetAttrString(deque, "append");
    if (append == NULL) {
        Py_DECREF(deque);
        return NULL;
    }

    for (uint64_t i = 0; i < count; i++) {
        PyObject *elem = read_string_object(fp);
        if (elem == NULL) {
            Py_DECREF(append);
            Py_DECREF(deque);
            return NULL;
        }
        PyObject *rc = PyObject_CallOneArg(append, elem);
        Py_DECREF(elem);
        if (rc == NULL) {
            Py_DECREF(append);
            Py_DECREF(deque);
            return NULL;
        }
        Py_DECREF(rc);
    }
    Py_DECREF(append);
    return deque;
}

static PyObject *
decode_hash(FILE *fp)
{
    uint64_t count;
    if (read_length(fp, &count) < 0) return NULL;

    PyObject *d = PyDict_New();
    if (d == NULL) return NULL;

    for (uint64_t i = 0; i < count; i++) {
        PyObject *field = read_string_object(fp);
        if (field == NULL) { Py_DECREF(d); return NULL; }
        PyObject *val = read_string_object(fp);
        if (val == NULL) { Py_DECREF(field); Py_DECREF(d); return NULL; }
        int rc = PyDict_SetItem(d, field, val);
        Py_DECREF(field);
        Py_DECREF(val);
        if (rc < 0) { Py_DECREF(d); return NULL; }
    }
    return d;
}

static PyObject *
decode_zset(FILE *fp)
{
    uint64_t count;
    if (read_length(fp, &count) < 0) return NULL;

    PyObject *ss = PyObject_CallNoArgs(cached_sorted_set_type);
    if (ss == NULL) return NULL;

    PyObject *insert = PyObject_GetAttrString(ss, "insert");
    if (insert == NULL) { Py_DECREF(ss); return NULL; }

    for (uint64_t i = 0; i < count; i++) {
        PyObject *member = read_string_object(fp);
        if (member == NULL) { Py_DECREF(insert); Py_DECREF(ss); return NULL; }
        double score;
        if (read_double_le(fp, &score) < 0) {
            Py_DECREF(member);
            Py_DECREF(insert);
            Py_DECREF(ss);
            return NULL;
        }
        PyObject *score_obj = PyFloat_FromDouble(score);
        if (score_obj == NULL) {
            Py_DECREF(member);
            Py_DECREF(insert);
            Py_DECREF(ss);
            return NULL;
        }
        /* SortedSet.insert(self, score, member) -- bound method, so
         * we just pass (score, member). */
        PyObject *rc = PyObject_CallFunctionObjArgs(insert, score_obj,
                                                    member, NULL);
        Py_DECREF(score_obj);
        Py_DECREF(member);
        if (rc == NULL) {
            Py_DECREF(insert);
            Py_DECREF(ss);
            return NULL;
        }
        Py_DECREF(rc);
    }
    Py_DECREF(insert);
    return ss;
}


/* =======================================================================
 * rdb_load(path) -> (data, ttl)
 * ======================================================================= */

static PyObject *
rdb_load(PyObject *self, PyObject *args)
{
    const char *path;
    if (!PyArg_ParseTuple(args, "s", &path)) return NULL;

    FILE *fp = fopen(path, "rb");
    if (fp == NULL) {
        PyErr_SetFromErrnoWithFilename(PyExc_OSError, path);
        return NULL;
    }

    PyObject *data = NULL, *ttl = NULL, *result = NULL;

    /* Header: 9 bytes total (5 magic + 4 ASCII version). */
    char header[9];
    if (read_bytes(fp, header, 9) < 0) goto out;
    if (memcmp(header, "REDIS", 5) != 0) {
        PyErr_SetString(PyExc_ValueError,
                        "missing REDIS magic at start of RDB file");
        goto out;
    }
    /* Version (header[5..8]) accepted as-is; we only emit "0011". */

    data = PyDict_New();
    if (data == NULL) goto out;
    ttl = PyDict_New();
    if (ttl == NULL) goto out;

    /* Pending TTL applies to the next key encountered. */
    int has_pending_ttl = 0;
    uint64_t pending_ttl_ms = 0;

    while (1) {
        uint8_t op;
        if (read_byte(fp, &op) < 0) goto out;

        if (op == RDB_OPCODE_EOF) {
            /* Read and discard the 8-byte trailer (CRC or zeros). */
            uint8_t trailer[8];
            if (read_bytes(fp, trailer, 8) < 0) goto out;
            break;
        }
        if (op == RDB_OPCODE_SELECTDB) {
            uint64_t db_number;
            if (read_length(fp, &db_number) < 0) goto out;
            /* Only DB 0 is meaningful for miniredis; ignore the number. */
            continue;
        }
        if (op == RDB_OPCODE_EXPIRETIME_MS) {
            if (read_uint64_le(fp, &pending_ttl_ms) < 0) goto out;
            has_pending_ttl = 1;
            continue;
        }

        /* Otherwise `op` is a type byte for the next key/value pair. */
        PyObject *key = read_string_object(fp);
        if (key == NULL) goto out;

        PyObject *value = NULL;
        switch (op) {
            case RDB_TYPE_STRING: value = decode_string(fp); break;
            case RDB_TYPE_LIST:   value = decode_list(fp);   break;
            case RDB_TYPE_HASH:   value = decode_hash(fp);   break;
            case RDB_TYPE_ZSET_2: value = decode_zset(fp);   break;
            default:
                Py_DECREF(key);
                PyErr_Format(PyExc_ValueError,
                             "unsupported RDB type byte 0x%02X", op);
                goto out;
        }
        if (value == NULL) {
            Py_DECREF(key);
            goto out;
        }

        int rc = PyDict_SetItem(data, key, value);
        Py_DECREF(value);
        if (rc < 0) {
            Py_DECREF(key);
            goto out;
        }

        if (has_pending_ttl) {
            /* Store TTL as absolute unix seconds to match store._ttl. */
            PyObject *ttl_seconds =
                PyFloat_FromDouble((double)pending_ttl_ms / 1000.0);
            if (ttl_seconds == NULL) {
                Py_DECREF(key);
                goto out;
            }
            rc = PyDict_SetItem(ttl, key, ttl_seconds);
            Py_DECREF(ttl_seconds);
            if (rc < 0) {
                Py_DECREF(key);
                goto out;
            }
            has_pending_ttl = 0;
            pending_ttl_ms = 0;
        }

        Py_DECREF(key);
    }

    result = PyTuple_Pack(2, data, ttl);

out:
    if (fp) fclose(fp);
    Py_XDECREF(data);
    Py_XDECREF(ttl);
    return result;  /* NULL on error (exception set); tuple on success. */
}


/* =======================================================================
 * Module bootstrap.
 * ======================================================================= */

static PyMethodDef RdbMethods[] = {
    {"dump", rdb_dump, METH_VARARGS,
     "dump(data: dict, ttl: dict, path: str) -> None\n\n"
     "Atomically write an RDB-format snapshot containing every key in\n"
     "`data` (bytes, deque, dict, or SortedSet values), with optional\n"
     "per-key TTLs from `ttl` (key -> absolute unix-seconds float).\n"
     "Writes to <path>.tmp, fsyncs, and renames over `path`."},

    {"load", rdb_load, METH_VARARGS,
     "load(path: str) -> tuple[dict, dict]\n\n"
     "Read an RDB-format snapshot. Returns (data, ttl) where ttl maps\n"
     "each key with an expiry to an absolute unix-seconds float."},

    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef rdbmodule = {
    PyModuleDef_HEAD_INIT,
    "miniredis._rdb",
    "RDB-format snapshot encoder and decoder.",
    -1,
    RdbMethods,
    NULL, NULL, NULL, NULL
};

PyMODINIT_FUNC
PyInit__rdb(void)
{
    PyObject *m = PyModule_Create(&rdbmodule);
    if (m == NULL) return NULL;

    /* Cache collections.deque type. */
    PyObject *collections = PyImport_ImportModule("collections");
    if (collections == NULL) {
        Py_DECREF(m);
        return NULL;
    }
    cached_deque_type = PyObject_GetAttrString(collections, "deque");
    Py_DECREF(collections);
    if (cached_deque_type == NULL) {
        Py_DECREF(m);
        return NULL;
    }

    /* Cache miniredis.custom_data_structures.SortedSet type. */
    PyObject *cds = PyImport_ImportModule("miniredis.custom_data_structures");
    if (cds == NULL) {
        Py_CLEAR(cached_deque_type);
        Py_DECREF(m);
        return NULL;
    }
    cached_sorted_set_type = PyObject_GetAttrString(cds, "SortedSet");
    Py_DECREF(cds);
    if (cached_sorted_set_type == NULL) {
        Py_CLEAR(cached_deque_type);
        Py_DECREF(m);
        return NULL;
    }

    return m;
}
