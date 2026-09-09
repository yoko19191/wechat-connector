"""Optional real Frida test against a synthetic CommonCrypto process, never WeChat."""

import hashlib
import importlib.util
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest


@unittest.skipUnless(importlib.util.find_spec('frida'), 'requires the optional init test environment')
class NativeCaptureTest(unittest.TestCase):
    def test_commoncrypto_capture_authenticates_sqlcipher_page(self):
        from wechat_connector.capture import capture_process
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            salt = bytes(range(16))
            key = hashlib.pbkdf2_hmac('sha512', b'fixture-only', salt, 256000, 32)
            database = root/'fixture.db'
            sql = (f'PRAGMA key="x\'{key.hex()}{salt.hex()}\'";\n'
                   'CREATE TABLE fixture(id INTEGER); INSERT INTO fixture VALUES(1);\n')
            subprocess.run(['sqlcipher','-noinit','-batch','-bail',str(database)],input=sql,
                           capture_output=True,text=True,check=True)
            page = database.read_bytes()[:4096]
            self.assertEqual(page[:16], salt)
            source = root/'probe.c'
            source.write_text('''#include <CommonCrypto/CommonKeyDerivation.h>
#include <unistd.h>
int main(void) {
    unsigned char salt[16], output[32];
    for (int i=0;i<16;i++) salt[i]=i;
    int result=CCKeyDerivationPBKDF(kCCPBKDF2,"fixture-only",12,salt,16,kCCPRFHmacAlgSHA512,256000,output,32);
    sleep(2);
    return result;
}
''')
            binary = root/'wechat-key-fixture'
            subprocess.run(['xcrun','clang',str(source),'-o',str(binary)],check=True,capture_output=True)
            notes = []
            result = capture_process(binary, {'fixture':page}, seconds=30, notify=notes.append)
            self.assertEqual(result, {'fixture':{'key':key,'salt':salt}})
            self.assertNotIn(key.hex(), '\n'.join(notes))
            self.assertFalse(any(root.glob('*keys*.jsonl')))


if __name__ == '__main__':
    unittest.main()
