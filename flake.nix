{
  description = "Listening Room: a quiet GTK4 room for radio, music libraries and ambience";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      lib = nixpkgs.lib;
      systems = [ "x86_64-linux" ];
      forSystems = f: lib.genAttrs systems f;

      # The application is GPL-3.0-or-later. The optional MTG model weights are
      # CC BY-NC-SA 4.0 (unfree); permit exactly those two for this flake.
      modelsFor =
        system:
        let
          pkgs = import nixpkgs {
            inherit system;
            config.allowUnfreePredicate =
              pkg:
              builtins.elem (lib.getName pkg) [
                "listening-room-voice-model"
                "listening-room-musicnn-model"
              ];
          };
        in
        pkgs.callPackage ./nix/models.nix { };

      build =
        pkgs:
        pkgs.callPackage ./nix/package.nix {
          inherit (modelsFor pkgs.stdenv.hostPlatform.system) voiceModel embeddingModel;
        };
    in
    {
      # Use as `nixpkgs.overlays = [ listening-room.overlays.default ];`, then
      # `pkgs.listening-hub.override { jellyfinUrl = "https://..."; }` to prefill
      # the packaged Jellyfin client.
      overlays.default = final: _prev: { listening-hub = build final; };

      packages = forSystems (
        system:
        let
          package = build nixpkgs.legacyPackages.${system};
        in
        {
          default = package;
          listening-hub = package;
        }
      );

      # `nix run` opens the window; `nix run .#listening-hub` is the menu CLI.
      apps = forSystems (system: {
        default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/listening-room";
        };
        listening-hub = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/listening-hub";
        };
      });

      checks = forSystems (system: {
        listening-hub = self.packages.${system}.default;
      });

      devShells = forSystems (
        system:
        let
          p = nixpkgs.legacyPackages.${system};
          package = self.packages.${system}.default;
        in
        {
          default = p.mkShell {
            packages = [
              package.python
              p.xvfb-run
              p.dbus
              p.mpv
              p.gitleaks
              p.ffmpeg
            ];
            nativeBuildInputs = [
              p.gobject-introspection
              p.wrapGAppsHook4
            ];
            # The package's own GStreamer plugins: the suites and acceptance play scene
            # loops like the installed window (the setup hook sets GST_PLUGIN_SYSTEM_PATH_1_0).
            buildInputs = [
              p.gtk4
              p.libadwaita
            ]
            ++ package.gstPlugins;
          };
        }
      );
    };
}
