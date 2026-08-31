import 'dart:async';
import 'dart:io';
import 'dart:typed_data';

import 'package:flutter/foundation.dart';
import 'package:intl/intl.dart';
import 'package:opus_dart/opus_dart.dart';
import 'package:vad/vad.dart';

/// Continuously reassembles BLE packets into Opus frames, decodes each one
/// as it arrives, feeds the decoded PCM into a real Silero VAD model (via
/// the `vad` package's custom-audio-stream support - our audio comes from
/// the Omi over BLE, not the phone's own microphone, which is exactly the
/// "non-microphone source" use case that package is built for), and
/// auto-saves a WAV file whenever VAD confirms a real gap in speech.
///
/// This replaces the earlier RMS-energy threshold entirely - that
/// approach couldn't distinguish "your voice, muffled through a shirt"
/// from "someone else talking normally across the room," since both
/// landed in the same volume range in real testing. A proper VAD model
/// judges the acoustic shape of speech, not just loudness, which is the
/// same category of tool Omi's own backend uses for this.
///
/// Frame reassembly (packet index + internal frame id fields, per the
/// firmware's transport.c), Opus decoding, and WAV writing are unchanged
/// from the version confirmed working against real hardware earlier this
/// session - only the speech/silence decision changed.
class SegmentingAudioWriter {
  final String outputDir;
  final void Function(File savedFile, int frameCount) onSegmentSaved;
  final void Function(String status) onStatus;

  final Duration silenceGap;
  final Duration minimumSegmentLength;
  final Duration maxSegmentLength;

  SegmentingAudioWriter({
    required this.outputDir,
    required this.onSegmentSaved,
    required this.onStatus,
    this.silenceGap = const Duration(seconds: 60),
    this.minimumSegmentLength = const Duration(seconds: 3),
    this.maxSegmentLength = const Duration(minutes: 10),
  }) {
    debugPrint('[SegmentingAudioWriter] Constructor entered, initializing VAD...');
    _initVad();
  }

  late final SimpleOpusDecoder _decoder = SimpleOpusDecoder(sampleRate: 16000, channels: 1);
  late final VadHandler _vad;
  final StreamController<Uint8List> _pcmStreamController = StreamController<Uint8List>.broadcast();
  Timer? _silenceCheckTimer;

  List<int> _pending = [];
  int _lastPacketIndex = -1;
  int _lastFrameId = -1;
  int lostFrames = 0;
  int _frameCount = 0;

  final List<int> _segmentSamples = [];
  DateTime? _segmentStartedAt;
  DateTime _lastSpeechAt = DateTime.now();
  bool _hasSpeechThisSegment = false;

  // Force-record mode: bypasses VAD/silence-gap entirely, records for a
  // fixed duration regardless of whether any speech is ever detected
  // (e.g. a presentation you're attending but not talking in), and saves
  // with a "-forced" filename marker so a future Thor-side no-voice
  // filter (issue #58) knows to skip this file rather than discard or
  // deprioritize it.
  bool _forceRecording = false;
  DateTime? _forceRecordDeadline;

  void _initVad() {
    try {
      // Created fresh here, in whichever isolate this class is
      // constructed in - given the exact same class of isolate-
      // initialization bug we hit with Opus earlier this session
      // (initOpus() called in the main isolate never reached the
      // separate foreground-service isolate), this needs to happen
      // wherever SegmentingAudioWriter itself actually runs, not assumed
      // to carry over from anywhere else.
      _vad = VadHandler.create(isDebug: true);
      debugPrint('[SegmentingAudioWriter] VadHandler.create() succeeded');

      _vad.onRealSpeechStart.listen((_) {
        _lastSpeechAt = DateTime.now();
        _hasSpeechThisSegment = true;
        onStatus('Listening... (speech detected)');
      });

      _vad.onVADMisfire.listen((_) {
        debugPrint('[SegmentingAudioWriter] VAD misfire (brief non-speech sound ignored)');
      });

      _vad.onError.listen((message) {
        debugPrint('[SegmentingAudioWriter] VAD error: $message');
      });

      _vad.startListening(audioStream: _pcmStreamController.stream).then((_) {
        debugPrint('[SegmentingAudioWriter] VAD startListening() completed successfully');
      }).catchError((e, stackTrace) {
        debugPrint('[SegmentingAudioWriter] VAD startListening() FAILED: $e\n$stackTrace');
      });

      _silenceCheckTimer = Timer.periodic(const Duration(seconds: 2), (_) => _checkSilenceGap());
      debugPrint('[SegmentingAudioWriter] _initVad() completed, silence-check timer running');
    } catch (e, stackTrace) {
      debugPrint('[SegmentingAudioWriter] _initVad() THREW: $e\n$stackTrace');
    }
  }

  bool _loggedFirstPacket = false;

  /// Starts a fixed-duration recording that ignores VAD/silence-gap
  /// entirely - saves regardless of whether any speech is ever detected,
  /// for situations like a presentation you're attending but not
  /// speaking in. Flushes whatever's currently accumulating as its own
  /// normal segment first, so forced and normal content never end up
  /// mixed together in one file.
  Future<void> startForceRecording(Duration duration) async {
    if (_hasSpeechThisSegment && _segmentSamples.isNotEmpty) {
      await _finalizeSegment();
    } else {
      _resetSegment();
    }
    _forceRecording = true;
    _forceRecordDeadline = DateTime.now().add(duration);
    _segmentStartedAt = DateTime.now();
    onStatus('Force recording for ${duration.inMinutes} min...');
  }

  /// Feed one raw BLE notification's bytes in. Call this from the
  /// audioDataStreamCharacteristic's notification listener.
  void addPacket(List<int> value) {
    if (!_loggedFirstPacket) {
      _loggedFirstPacket = true;
      debugPrint('[SegmentingAudioWriter] addPacket() received its first packet - data is reaching this class.');
    }
    if (value.length < 3) return;

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
      debugPrint('[SegmentingAudioWriter] Lost packet, discarding in-progress frame');
      _lastPacketIndex = -1;
      _pending = [];
      lostFrames += 1;
      return;
    }

    if (internal == 0) {
      _onFrameComplete(_pending);
      _pending = content;
      _lastFrameId = internal;
      _lastPacketIndex = index;
      return;
    }

    _pending.addAll(content);
    _lastFrameId = internal;
    _lastPacketIndex = index;
  }

  void _onFrameComplete(List<int> frame) {
    if (frame.isEmpty) return;

    List<int> samples;
    try {
      samples = _decoder.decode(input: Uint8List.fromList(frame));
    } catch (e) {
      debugPrint('[SegmentingAudioWriter] Failed to decode a frame, skipping it: $e');
      return;
    }

    _frameCount++;
    _segmentStartedAt ??= DateTime.now();
    _segmentSamples.addAll(samples);

    // Feed the same decoded PCM into VAD, as PCM16 little-endian bytes -
    // the format the package's custom-audio-stream support expects.
    // Skipped entirely during force-recording, where the speech/silence
    // decision doesn't matter - we're saving regardless.
    if (!_forceRecording && !_pcmStreamController.isClosed) {
      _pcmStreamController.add(_pcmToLittleEndianBytes(samples));
    }
  }

  void _checkSilenceGap() {
    if (_segmentStartedAt == null) return; // nothing accumulating yet

    final now = DateTime.now();

    if (_forceRecording) {
      if (now.isAfter(_forceRecordDeadline!)) {
        onStatus('Force-record duration reached - saving...');
        _finalizeSegment(forced: true);
        _forceRecording = false;
        _forceRecordDeadline = null;
      } else {
        final remaining = _forceRecordDeadline!.difference(now);
        onStatus('Force recording... ${remaining.inMinutes}m ${remaining.inSeconds % 60}s remaining');
      }
      return; // normal VAD-driven silence logic doesn't apply here
    }

    // Hard ceiling check first, independent of VAD state entirely - same
    // safety net as before, still needed regardless of how good the
    // speech detection itself is, since even correctly-recognized
    // sustained ambient conversation nearby would otherwise never hit a
    // clean gap either.
    if (now.difference(_segmentStartedAt!) >= maxSegmentLength) {
      if (_hasSpeechThisSegment) {
        onStatus('Reached max segment length - saving and starting fresh...');
        _finalizeSegment();
      } else {
        _resetSegment();
      }
      return;
    }

    final silentFor = now.difference(_lastSpeechAt);
    if (_hasSpeechThisSegment) {
      onStatus('Listening... (quiet for ${silentFor.inSeconds}s)');
    }

    if (silentFor >= silenceGap && _hasSpeechThisSegment) {
      final segmentLength = now.difference(_segmentStartedAt!);
      if (segmentLength >= minimumSegmentLength) {
        _finalizeSegment();
      } else {
        _resetSegment();
      }
    }
  }

  void _resetSegment() {
    _segmentSamples.clear();
    _segmentStartedAt = null;
    _hasSpeechThisSegment = false;
    _frameCount = 0;
  }

  Future<void> _finalizeSegment({bool forced = false}) async {
    onStatus('Saving segment...');
    final samplesCopy = List<int>.from(_segmentSamples);
    final savedFrameCount = _frameCount;
    _resetSegment();

    try {
      final pcmBytes = _pcmToLittleEndianBytes(samplesCopy);
      final wavBytes = _wrapWithWavHeader(pcmBytes, sampleRate: 16000);
      final suffix = forced ? '-forced' : '';
      final filename = 'omi-${DateFormat('yyyyMMdd_HHmmss').format(DateTime.now())}$suffix.wav';
      final path = '$outputDir/$filename';
      final file = File(path);
      await file.parent.create(recursive: true);
      await file.writeAsBytes(wavBytes);
      onSegmentSaved(file, savedFrameCount);
      onStatus('Saved: $filename - listening for next conversation...');
    } catch (e) {
      onStatus('Failed to write file: $e');
    }
  }

  /// Call when stopping capture entirely (app closing, device
  /// disconnecting) - flushes whatever's currently accumulated as a
  /// final segment, rather than silently discarding a partial
  /// conversation just because it hadn't hit a silence gap yet.
  Future<void> flushCurrentSegment() async {
    if (_forceRecording && _segmentSamples.isNotEmpty) {
      // Force-recorded content saves regardless of detected speech -
      // that's the whole point of this mode - so the normal
      // _hasSpeechThisSegment gate below doesn't apply here.
      await _finalizeSegment(forced: true);
      _forceRecording = false;
      _forceRecordDeadline = null;
    } else if (_hasSpeechThisSegment && _segmentSamples.isNotEmpty) {
      await _finalizeSegment();
    }
  }

  Future<void> dispose() async {
    _silenceCheckTimer?.cancel();
    await _vad.dispose();
    await _pcmStreamController.close();
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
    header.setUint8(0, 0x52);
    header.setUint8(1, 0x49);
    header.setUint8(2, 0x46);
    header.setUint8(3, 0x46);
    header.setUint32(4, chunkSize, Endian.little);
    header.setUint8(8, 0x57);
    header.setUint8(9, 0x41);
    header.setUint8(10, 0x56);
    header.setUint8(11, 0x45);
    header.setUint8(12, 0x66);
    header.setUint8(13, 0x6D);
    header.setUint8(14, 0x74);
    header.setUint8(15, 0x20);
    header.setUint32(16, 16, Endian.little);
    header.setUint16(20, 1, Endian.little);
    header.setUint16(22, channels, Endian.little);
    header.setUint32(24, sampleRate, Endian.little);
    header.setUint32(28, byteRate, Endian.little);
    header.setUint16(32, blockAlign, Endian.little);
    header.setUint16(34, bitsPerSample, Endian.little);
    header.setUint8(36, 0x64);
    header.setUint8(37, 0x61);
    header.setUint8(38, 0x74);
    header.setUint8(39, 0x61);
    header.setUint32(40, dataSize, Endian.little);

    return Uint8List.fromList(header.buffer.asUint8List() + pcmData);
  }
}
