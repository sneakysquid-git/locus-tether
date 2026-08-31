import 'dart:io';
import 'dart:typed_data';

import 'package:flutter/foundation.dart';
import 'package:intl/intl.dart';
import 'package:opus_dart/opus_dart.dart';

/// Reassembles raw BLE packets into Opus frames, decodes them, and writes
/// a standard WAV file - the same core logic as my-omi's WavBytesUtil,
/// narrowed to only the Opus path (the one actually confirmed against
/// real hardware this session, via the WAL-saving test that happened
/// mid-toolchain-fight).
///
/// Packet structure (per my-omi's storeFramePacket, itself derived from
/// the firmware's transport.c): each BLE notification is
///   [packet_index_low, packet_index_high, internal_frame_id, ...payload]
/// Multiple packets with consecutive internal_frame_id values belong to
/// ONE logical Opus frame; internal_frame_id resetting to 0 marks the
/// start of the next frame. A gap in packet_index means a dropped
/// packet - the in-progress frame is discarded rather than silently
/// corrupted.
class OmiAudioWriter {
  final List<List<int>> _frames = [];
  List<int> _pending = [];
  int _lastPacketIndex = -1;
  int _lastFrameId = -1;
  int lostFrames = 0;

  late final SimpleOpusDecoder _decoder;

  OmiAudioWriter() {
    // Omi's firmware encodes at 16kHz mono - confirmed both in my-omi's
    // decoder setup and independently via this session's earlier
    // firmware source analysis (PDM mic -> Opus encode pipeline).
    _decoder = SimpleOpusDecoder(sampleRate: 16000, channels: 1);
  }

  /// Feed one raw BLE notification's bytes in. Call this from the
  /// audioDataStreamCharacteristic's notification listener.
  void addPacket(List<int> value) {
    if (value.length < 3) return; // too short to contain the header bytes

    final int index = value[0] + (value[1] << 8);
    final int internal = value[2];
    final List<int> content = value.sublist(3);

    if (_lastPacketIndex == -1 && internal == 0) {
      _lastPacketIndex = index;
      _lastFrameId = internal;
      _pending = content;
      return;
    }

    if (_lastPacketIndex == -1) return;

    final bool lostPacket = index != _lastPacketIndex + 1 || (internal != 0 && internal != _lastFrameId + 1);
    if (lostPacket) {
      debugPrint('[OmiAudioWriter] Lost packet, discarding in-progress frame');
      _lastPacketIndex = -1;
      _pending = [];
      lostFrames += 1;
      return;
    }

    if (internal == 0) {
      _frames.add(_pending);
      _pending = content;
      _lastFrameId = internal;
      _lastPacketIndex = index;
      return;
    }

    _pending.addAll(content);
    _lastFrameId = internal;
    _lastPacketIndex = index;
  }

  void _finalizePendingFrame() {
    if (_pending.isNotEmpty) {
      _frames.add(List<int>.from(_pending));
      _pending = [];
      _lastPacketIndex = -1;
      _lastFrameId = -1;
    }
  }

  int get frameCount => _frames.length;

  /// Decode everything accumulated so far and write it as a WAV file at
  /// the given absolute path. Clears accumulated frames afterward, so
  /// this is meant to be called once per recording session, not
  /// continuously mid-stream.
  ///
  /// The output path is the one genuinely UNTESTED piece of this whole
  /// port - everything else here mirrors confirmed my-omi behavior, but
  /// writing to shared/external storage from Flutter on modern Android
  /// depends on permissions and scoped-storage rules that need a real
  /// on-device test to confirm, not just code review.
  Future<File> writeWavFile(String outputPath) async {
    _finalizePendingFrame();

    if (_frames.isEmpty) {
      throw StateError('No audio frames captured - nothing to write.');
    }

    final List<int> decodedSamples = [];
    for (final frame in _frames) {
      try {
        decodedSamples.addAll(_decoder.decode(input: Uint8List.fromList(frame)));
      } catch (e) {
        debugPrint('[OmiAudioWriter] Failed to decode one frame, skipping it: $e');
        // Skip the bad frame rather than aborting the whole recording -
        // one corrupt frame shouldn't discard everything else captured.
      }
    }

    _frames.clear();

    if (decodedSamples.isEmpty) {
      throw StateError('All frames failed to decode - nothing to write.');
    }

    final pcmBytes = _pcmToLittleEndianBytes(decodedSamples);
    final wavBytes = _wrapWithWavHeader(pcmBytes, sampleRate: 16000);

    final file = File(outputPath);
    await file.parent.create(recursive: true);
    await file.writeAsBytes(wavBytes);
    debugPrint('[OmiAudioWriter] Wrote ${wavBytes.length} bytes to $outputPath');
    return file;
  }

  static Uint8List _pcmToLittleEndianBytes(List<int> samples) {
    final byteData = ByteData(2 * samples.length);
    for (int i = 0; i < samples.length; i++) {
      byteData.setInt16(i * 2, samples[i], Endian.little);
    }
    return byteData.buffer.asUint8List();
  }

  static Uint8List _wrapWithWavHeader(Uint8List pcmData, {required int sampleRate, int channels = 1, int bitsPerSample = 16}) {
    final int byteRate = sampleRate * channels * bitsPerSample ~/ 8;
    final int blockAlign = channels * bitsPerSample ~/ 8;
    final int dataSize = pcmData.length;
    final int chunkSize = 36 + dataSize;

    final header = ByteData(44);
    // "RIFF"
    header.setUint8(0, 0x52);
    header.setUint8(1, 0x49);
    header.setUint8(2, 0x46);
    header.setUint8(3, 0x46);
    header.setUint32(4, chunkSize, Endian.little);
    // "WAVE"
    header.setUint8(8, 0x57);
    header.setUint8(9, 0x41);
    header.setUint8(10, 0x56);
    header.setUint8(11, 0x45);
    // "fmt "
    header.setUint8(12, 0x66);
    header.setUint8(13, 0x6D);
    header.setUint8(14, 0x74);
    header.setUint8(15, 0x20);
    header.setUint32(16, 16, Endian.little);
    header.setUint16(20, 1, Endian.little); // PCM
    header.setUint16(22, channels, Endian.little);
    header.setUint32(24, sampleRate, Endian.little);
    header.setUint32(28, byteRate, Endian.little);
    header.setUint16(32, blockAlign, Endian.little);
    header.setUint16(34, bitsPerSample, Endian.little);
    // "data"
    header.setUint8(36, 0x64);
    header.setUint8(37, 0x61);
    header.setUint8(38, 0x74);
    header.setUint8(39, 0x61);
    header.setUint32(40, dataSize, Endian.little);

    return Uint8List.fromList(header.buffer.asUint8List() + pcmData);
  }

  static String defaultFilename() {
    return 'omi-${DateFormat('yyyyMMdd_HHmmss').format(DateTime.now())}.wav';
  }
}
