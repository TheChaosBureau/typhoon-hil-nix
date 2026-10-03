{
  description = "Typhoon HIL Control Center and Virtual HIL from a container: image recipe, workstation launcher and CI entry point";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

  outputs = { self, nixpkgs }:
    let
      # Control Center's Linux build is x86_64 only.
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
      typhoon-hil = pkgs.callPackage ./package.nix { };
      testPython = pkgs.python3.withPackages (ps: [ ps.pytest ]);
    in
    {
      packages.${system} = {
        inherit typhoon-hil;
        default = typhoon-hil;
      };

      apps.${system}.default = {
        type = "app";
        program = "${typhoon-hil}/bin/typhoon-hil";
        meta.description = typhoon-hil.meta.description;
      };

      # For a repository whose dev shell or system should carry the launcher:
      #   nixpkgs.overlays = [ typhoon-hil-nix.overlays.default ];
      overlays.default = final: _prev: {
        typhoon-hil = final.callPackage ./package.nix { };
      };

      checks.${system} = {
        # Builds the package, which runs shellcheck over every script.
        package = typhoon-hil;

        # Everything that can be proven without podman, Typhoon or a license.
        tests = pkgs.runCommand "typhoon-hil-tests"
          { nativeBuildInputs = [ testPython pkgs.bash pkgs.util-linux ]; }
          ''
            cp -r ${self} src
            chmod -R u+w src
            cd src
            python3 -m pytest -q -p no:cacheprovider tests
            touch $out
          '';
      };

      devShells.${system}.default = pkgs.mkShell {
        packages = [ testPython pkgs.shellcheck ];
      };
    };
}
