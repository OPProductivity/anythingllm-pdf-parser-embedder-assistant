"""Stage CLI export sets before publishing, with explicit overwrite consent."""
from contextlib import redirect_stdout
from functools import wraps
import argparse
import io
import os
import shutil
from pathlib import Path
import tempfile


def staged_cli_exports(kind, stem_function):
    def decorate(function):
        @wraps(function)
        def run(args):
            output = Path(args.out_dir)
            output.mkdir(parents=True, exist_ok=True)
            base = stem_function(args.output_base_name or Path(args.pdf).stem)
            backend = stem_function(args.backend.lower())
            suffixes = (["extract.txt", "page-report.csv", "extract-validation.csv", "elements.csv"]
                        if kind == "extract" else ["page-segmented.txt", "page-segmented-report.csv", "validation.csv", "elements.csv"])
            targets = [output / f"{base}-{backend}-{suffix}" for suffix in suffixes]
            overwrite = bool(getattr(args, "overwrite", False))
            reserved = {}
            published = []
            backups = []
            captured = io.StringIO()
            staging = Path(tempfile.mkdtemp(prefix=".pdf-export-", dir=output))
            preserve_staging = False
            try:
                try:
                    for target in targets:
                        if overwrite:
                            if target.is_symlink() or (target.exists() and not target.is_file()):
                                raise FileExistsError(f"Refusing non-file export destination: {target}")
                        else:
                            # Exclusive reservation rejects existing files and
                            # symlinks before any expensive extraction starts.
                            with target.open("xb"):
                                pass
                            reserved[target] = target.stat().st_ino
                    staged_args = argparse.Namespace(**vars(args))
                    staged_args.out_dir = str(staging)
                    with redirect_stdout(captured):
                        result = function(staged_args)
                    for target in targets:
                        candidate = staging / target.name
                        if not candidate.is_file():
                            continue
                        if not overwrite and (target.is_symlink() or target.stat().st_ino != reserved[target]):
                            raise FileExistsError(f"Export reservation changed: {target}")
                        if overwrite and target.exists():
                            backup = staging / (target.name + ".previous")
                            os.replace(target, backup)
                            backups.append((target, backup))
                        os.replace(candidate, target)
                        published.append(target)
                except BaseException:
                    try:
                        for target in published:
                            target.unlink(missing_ok=True)
                        for target, backup in reversed(backups):
                            os.replace(backup, target)
                    except OSError as rollback_error:
                        preserve_staging = True
                        raise OSError(f"Export rollback could not finish; original backup files are retained in {staging}") from rollback_error
                    raise
                finally:
                    for target, inode in reserved.items():
                        if target not in published and target.exists() and target.stat().st_ino == inode:
                            target.unlink()
            finally:
                if not preserve_staging:
                    if staging.is_symlink() or staging.resolve().parent != output.resolve():
                        raise OSError("Refusing export cleanup outside its reserved output directory.")
                    shutil.rmtree(staging)
            print(captured.getvalue().replace(str(staging), str(output)), end="")
            return result
        return run
    return decorate
