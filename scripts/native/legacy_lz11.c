/*
 * Byte-exact Nintendo LZ11 decode and XY Legacy LZR2 recipe replay.
 *
 * This library is deliberately freestanding: it allocates no memory, uses no
 * C runtime functions, and writes only into caller-provided buffers.  That
 * makes the same source suitable for Windows, macOS, and Linux helpers.
 */

#include <stddef.h>
#include <stdint.h>

#if defined(_WIN32)
#define LEGACY_EXPORT __declspec(dllexport)
#else
#define LEGACY_EXPORT __attribute__((visibility("default")))
#endif

#define LEGACY_LZ11_API_VERSION 1u

static uint32_t read_u32_le(const uint8_t *data) {
    return ((uint32_t)data[0])
        | ((uint32_t)data[1] << 8)
        | ((uint32_t)data[2] << 16)
        | ((uint32_t)data[3] << 24);
}

static uint64_t read_u64_le(const uint8_t *data) {
    return ((uint64_t)data[0])
        | ((uint64_t)data[1] << 8)
        | ((uint64_t)data[2] << 16)
        | ((uint64_t)data[3] << 24)
        | ((uint64_t)data[4] << 32)
        | ((uint64_t)data[5] << 40)
        | ((uint64_t)data[6] << 48)
        | ((uint64_t)data[7] << 56);
}

LEGACY_EXPORT uint32_t legacy_lz11_api_version(void) {
    return LEGACY_LZ11_API_VERSION;
}

/*
 * Return 0 on success, -1 for invalid arguments or headers, -2 for a size
 * mismatch, -3 for truncated input, or -4 for an invalid back-reference.
 */
LEGACY_EXPORT int legacy_lz11_decode(
    const uint8_t *source,
    size_t source_size,
    uint8_t *destination,
    size_t destination_size,
    size_t *consumed_bytes
) {
    size_t input_position;
    size_t output_position;
    size_t raw_size;

    if (!source || !destination || !consumed_bytes || source_size < 4
        || source[0] != 0x11) {
        return -1;
    }
    raw_size = (size_t)source[1] | ((size_t)source[2] << 8)
               | ((size_t)source[3] << 16);
    if (raw_size == 0 || raw_size != destination_size) {
        return -2;
    }

    input_position = 4;
    output_position = 0;
    while (output_position < raw_size) {
        uint8_t flags;
        int bit;
        if (input_position >= source_size) {
            return -3;
        }
        flags = source[input_position++];

        if (flags == 0) {
            size_t count = raw_size - output_position;
            size_t i;
            if (count > 8) {
                count = 8;
            }
            if (count > source_size - input_position) {
                return -3;
            }
            for (i = 0; i < count; ++i) {
                destination[output_position + i] = source[input_position + i];
            }
            input_position += count;
            output_position += count;
            continue;
        }

        for (bit = 7; bit >= 0 && output_position < raw_size; --bit) {
            if ((flags & (uint8_t)(1u << bit)) == 0) {
                if (input_position >= source_size) {
                    return -3;
                }
                destination[output_position++] = source[input_position++];
            } else {
                uint8_t b1;
                uint32_t length;
                uint32_t displacement;
                size_t token_len;
                size_t actual;
                size_t distance;
                size_t source_position;
                size_t i;

                if (input_position >= source_size) {
                    return -3;
                }
                b1 = source[input_position];
                if ((b1 >> 4) == 0) {
                    uint8_t b2;
                    uint8_t b3;
                    if (source_size - input_position < 3) {
                        return -3;
                    }
                    b2 = source[input_position + 1];
                    b3 = source[input_position + 2];
                    length
                        = ((((uint32_t)b1 & 0x0fu) << 4) | ((uint32_t)b2 >> 4))
                          + 0x11u;
                    displacement
                        = (((uint32_t)b2 & 0x0fu) << 8) | (uint32_t)b3;
                    token_len = 3;
                } else if ((b1 >> 4) == 1) {
                    uint8_t b2;
                    uint8_t b3;
                    uint8_t b4;
                    if (source_size - input_position < 4) {
                        return -3;
                    }
                    b2 = source[input_position + 1];
                    b3 = source[input_position + 2];
                    b4 = source[input_position + 3];
                    length = ((((uint32_t)b1 & 0x0fu) << 12)
                              | ((uint32_t)b2 << 4) | ((uint32_t)b3 >> 4))
                             + 0x111u;
                    displacement
                        = (((uint32_t)b3 & 0x0fu) << 8) | (uint32_t)b4;
                    token_len = 4;
                } else {
                    uint8_t b2;
                    if (source_size - input_position < 2) {
                        return -3;
                    }
                    b2 = source[input_position + 1];
                    length = ((uint32_t)b1 >> 4) + 1u;
                    displacement
                        = (((uint32_t)b1 & 0x0fu) << 8) | (uint32_t)b2;
                    token_len = 2;
                }
                input_position += token_len;
                distance = (size_t)displacement + 1;
                if (distance == 0 || distance > output_position) {
                    return -4;
                }
                actual = (size_t)length;
                if (actual > raw_size - output_position) {
                    actual = raw_size - output_position;
                }
                source_position = output_position - distance;
                /* Forward byte copying intentionally implements LZ overlap. */
                for (i = 0; i < actual; ++i) {
                    destination[output_position + i]
                        = destination[source_position + i];
                }
                output_position += actual;
            }
        }
    }

    *consumed_bytes = input_position;
    return 0;
}

/*
 * Replay an uncompressed LZR2 recipe. Literal bytes are taken from raw; the
 * recipe contains only target flags and back-reference token bytes.
 *
 * Return 0 on success, -1 for invalid arguments or headers, -2 for malformed
 * lengths, -3 for truncated tokens, or -4 for a final cursor mismatch.
 */
LEGACY_EXPORT int legacy_lz11_rebuild_lzr2(
    const uint8_t *raw,
    size_t raw_len,
    const uint8_t *recipe,
    size_t recipe_len,
    uint8_t *destination,
    size_t destination_size
) {
    uint64_t raw_size64;
    uint64_t target_size64;
    uint32_t flags_len32;
    uint32_t refs_len32;
    size_t raw_size;
    size_t target_size;
    size_t flags_len;
    size_t refs_len;
    const uint8_t *flags;
    const uint8_t *refs;
    size_t flag_pos;
    size_t ref_pos;
    size_t output_position;
    size_t raw_pos;

    if (!raw || !recipe || !destination || recipe_len < 28 || recipe[0] != 'L'
        || recipe[1] != 'Z' || recipe[2] != 'R' || recipe[3] != '2') {
        return -1;
    }

    raw_size64 = read_u64_le(recipe + 4);
    target_size64 = read_u64_le(recipe + 12);
    flags_len32 = read_u32_le(recipe + 20);
    refs_len32 = read_u32_le(recipe + 24);
    raw_size = (size_t)raw_size64;
    target_size = (size_t)target_size64;
    flags_len = (size_t)flags_len32;
    refs_len = (size_t)refs_len32;

    if ((uint64_t)raw_size != raw_size64
        || (uint64_t)target_size != target_size64 || raw_size != raw_len
        || target_size != destination_size || flags_len > recipe_len - 28
        || refs_len != recipe_len - 28 - flags_len || raw_size == 0
        || target_size < 4) {
        return -2;
    }

    flags = recipe + 28;
    refs = flags + flags_len;
    destination[0] = 0x11;
    destination[1] = (uint8_t)(raw_size & 0xffu);
    destination[2] = (uint8_t)((raw_size >> 8) & 0xffu);
    destination[3] = (uint8_t)((raw_size >> 16) & 0xffu);
    output_position = 4;
    raw_pos = 0;
    ref_pos = 0;

    for (flag_pos = 0; flag_pos < flags_len && raw_pos < raw_size;
         ++flag_pos) {
        uint8_t flag = flags[flag_pos];
        int bit;
        if (output_position >= target_size) {
            return -4;
        }
        destination[output_position++] = flag;

        if (flag == 0) {
            size_t count = raw_size - raw_pos;
            size_t i;
            if (count > 8) {
                count = 8;
            }
            if (count > target_size - output_position) {
                return -4;
            }
            for (i = 0; i < count; ++i) {
                destination[output_position + i] = raw[raw_pos + i];
            }
            output_position += count;
            raw_pos += count;
            continue;
        }

        for (bit = 7; bit >= 0 && raw_pos < raw_size; --bit) {
            if ((flag & (uint8_t)(1u << bit)) == 0) {
                if (output_position >= target_size) {
                    return -4;
                }
                destination[output_position++] = raw[raw_pos++];
            } else {
                uint8_t b1;
                uint32_t length;
                size_t token_len;
                size_t actual;
                size_t i;

                if (ref_pos >= refs_len) {
                    return -3;
                }
                b1 = refs[ref_pos];
                if ((b1 >> 4) == 0) {
                    uint8_t b2;
                    if (refs_len - ref_pos < 3) {
                        return -3;
                    }
                    b2 = refs[ref_pos + 1];
                    length
                        = ((((uint32_t)b1 & 0x0fu) << 4) | ((uint32_t)b2 >> 4))
                          + 0x11u;
                    token_len = 3;
                } else if ((b1 >> 4) == 1) {
                    uint8_t b2;
                    uint8_t b3;
                    if (refs_len - ref_pos < 4) {
                        return -3;
                    }
                    b2 = refs[ref_pos + 1];
                    b3 = refs[ref_pos + 2];
                    length = ((((uint32_t)b1 & 0x0fu) << 12)
                              | ((uint32_t)b2 << 4) | ((uint32_t)b3 >> 4))
                             + 0x111u;
                    token_len = 4;
                } else {
                    if (refs_len - ref_pos < 2) {
                        return -3;
                    }
                    length = ((uint32_t)b1 >> 4) + 1u;
                    token_len = 2;
                }
                if (token_len > target_size - output_position) {
                    return -4;
                }
                for (i = 0; i < token_len; ++i) {
                    destination[output_position + i] = refs[ref_pos + i];
                }
                output_position += token_len;
                ref_pos += token_len;
                actual = (size_t)length;
                if (actual > raw_size - raw_pos) {
                    actual = raw_size - raw_pos;
                }
                raw_pos += actual;
            }
        }
    }

    if (raw_pos != raw_size || ref_pos != refs_len
        || output_position != target_size) {
        return -4;
    }
    return 0;
}
