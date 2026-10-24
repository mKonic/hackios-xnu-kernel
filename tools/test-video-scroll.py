#!/usr/bin/env python3
"""Check the XNU C scroll routines against guard pages and overlapping scanlines.

Usage: python3 tools/test-video-scroll.py /path/to/xnu/osfmk/console/video_scroll.c
"""
import argparse
from pathlib import Path
import subprocess
import tempfile

TEST = r'''
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>
int main(void) {
 size_t pg=getpagesize();
 unsigned char *m=mmap(NULL,pg*4,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANON,-1,0);
 if(m==MAP_FAILED) return 2;
 mprotect(m+3*pg,pg,PROT_NONE);
 size_t lengths[]={16,4608,5760};
 for(unsigned t=0;t<3;t++) {
  size_t n=lengths[t];
  unsigned char *a=m+3*pg-n, *dst=m+64;
  for(size_t i=0;i<pg*3;i++)m[i]=(unsigned char)(i*13+7);
  unsigned char *expected=malloc(pg*3); memcpy(expected,m,pg*3);
  memmove(expected+64,expected+(a-m),n);
  video_scroll_up(a,a+n,dst);
  if(memcmp(expected,m,pg*3))return 3;
  for(size_t i=0;i<pg*3;i++)m[i]=(unsigned char)(i*19+11);
  memcpy(expected,m,pg*3);
  unsigned char *lo=m+3*pg-n-32; unsigned char *hi=lo+n;
  memmove(expected+(lo-m)+16,expected+(lo-m),n);
  video_scroll_down(hi,lo,hi+16);
  if(memcmp(expected,m,pg*3))return 4;
  free(expected);
 }
 puts("PASS: scanline bounds, adjacent bytes, overlapping downward copy");
 return 0;
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    source = args.source.read_text()
    # The routines only need bcopy and size_t; retain their actual implementation.
    source = "\n".join(line for line in source.splitlines()
                       if not line.startswith("#include"))
    with tempfile.TemporaryDirectory(prefix="video-scroll-") as directory:
        path = Path(directory)
        test = path / "scroll.c"
        test.write_text("#include <stddef.h>\n#include <strings.h>\n" + source + TEST)
        executable = path / "scroll"
        subprocess.run(["clang", "-O2", str(test), "-o", str(executable)], check=True)
        subprocess.run([str(executable)], check=True)


if __name__ == "__main__":
    main()
