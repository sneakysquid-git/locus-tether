// Real, confirmed BLE UUIDs for the Omi device.
//
// Sourced directly from the my-omi fork's lib/services/models.dart, and
// independently cross-checked against DeepWiki's analysis of the actual
// BasedHardware/omi firmware/app source earlier this session (which
// specifically confirmed 19b10002 as the audioCodec characteristic) -
// two independent sources agreeing is a real, if not absolute,
// confirmation these are genuinely correct rather than fork-specific.
//
// Deliberately omits battery/button/storage/speaker/accelerometer/image
// service UUIDs - my-omi supports all of those, but this minimal capture
// app only needs the three below.

const String omiServiceUuid = '19b10000-e8f2-537e-4f6c-d104768a1214';
const String audioDataStreamCharacteristicUuid = '19b10001-e8f2-537e-4f6c-d104768a1214';
const String audioCodecCharacteristicUuid = '19b10002-e8f2-537e-4f6c-d104768a1214';

// Codec byte values as read from the audioCodec characteristic. The
// original my-omi mapping (1/10/20) was written against an older
// firmware version - Eric's device, freshly updated to Omi's newest
// official firmware this session, returned 21 instead of the expected
// 20 for what is very likely still Opus (close numeric proximity, no
// other plausible codec in context). Treating 21 as Opus here, but the
// real confirmation is whether captured audio actually decodes
// correctly with the Opus decoder - not the number itself.
enum OmiAudioCodec { pcm8, pcm16, opus, unknown }

OmiAudioCodec parseOmiCodec(int codecId) {
  switch (codecId) {
    case 1:
      return OmiAudioCodec.pcm8;
    case 10:
      return OmiAudioCodec.pcm16;
    case 20:
    case 21:
      return OmiAudioCodec.opus;
    default:
      return OmiAudioCodec.unknown;
  }
}
