import io,json,sys,tarfile,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
from restore import validate_backup
from backup import sha256_file
from bootstrap import Compose
class BackupTests(unittest.TestCase):
    def fixture(self,root,bad=False):
        (root/'postgres.dump').write_bytes(b'-- not executed\n')
        for name in ['scope.tar.gz','redis.tar.gz']:
            with tarfile.open(root/name,'w:gz') as archive:
                info=tarfile.TarInfo('../outside' if bad else 'safe/data');info.size=4;archive.addfile(info,io.BytesIO(b'data'))
        manifest={'format':1,'postgres_major':17,'redis_major':8,'files':{p.name:{'size':p.stat().st_size,'sha256':sha256_file(p)} for p in root.iterdir()}}
        (root/'manifest.json').write_text(json.dumps(manifest))
    def test_valid_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.fixture(root);self.assertEqual(validate_backup(root)['format'],1)
    def test_corrupt_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.fixture(root);(root/'postgres.dump').write_bytes(b'corrupt')
            with self.assertRaises(ValueError):validate_backup(root)
    def test_tar_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.fixture(root,True)
            with self.assertRaises(ValueError):validate_backup(root)
    def test_project_interpolation(self):
        self.assertEqual(Compose(Path('/tmp/test-no-secret.env'),'isolated-restore').environment['COMPOSE_PROJECT_NAME'],'isolated-restore')
