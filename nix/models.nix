# Optional voice/instrumental estimation models, fetched from the Music
# Technology Group (UPF) at build time. Model weights are CC BY-NC-SA 4.0,
# independently of the application's GPL-3.0-or-later source license; see
# src/models/README.md. The flake permits only these two unfree weights.
{ lib, fetchurl }:
{
  voiceModel = fetchurl {
    name = "listening-room-voice-model-2.onnx";
    url = "https://essentia.upf.edu/models/classification-heads/voice_instrumental/voice_instrumental-msd-musicnn-1.onnx";
    hash = "sha256-3QVEEPpPn8sAhrQiOppcZt0JjF1M70hcA68LBQBEO3M=";
    meta.license = lib.licenses.cc-by-nc-sa-40;
  };
  embeddingModel = fetchurl {
    name = "listening-room-musicnn-model-1.onnx";
    url = "https://essentia.upf.edu/models/feature-extractors/musicnn/msd-musicnn-1.onnx";
    hash = "sha256-SWaP/sR+UulLlvRZMLtGoooTaNS9+1wFN4+oNKymFuE=";
    meta.license = lib.licenses.cc-by-nc-sa-40;
  };
}
