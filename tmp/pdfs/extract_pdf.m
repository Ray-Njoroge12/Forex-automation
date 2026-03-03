#import <Foundation/Foundation.h>
#import <PDFKit/PDFKit.h>

int main(int argc, const char * argv[]) {
    @autoreleasepool {
        if (argc < 3) {
            fprintf(stderr, "usage: %s <input.pdf> <output.txt>\n", argv[0]);
            return 1;
        }
        NSString *inPath = [NSString stringWithUTF8String:argv[1]];
        NSString *outPath = [NSString stringWithUTF8String:argv[2]];
        NSURL *url = [NSURL fileURLWithPath:inPath];
        PDFDocument *doc = [[PDFDocument alloc] initWithURL:url];
        if (!doc) {
            fprintf(stderr, "failed to open PDF: %s\n", argv[1]);
            return 2;
        }

        NSMutableString *all = [NSMutableString stringWithFormat:@"FILE: %@\nPAGES: %ld\n", inPath, (long)doc.pageCount];
        for (NSInteger i = 0; i < doc.pageCount; i++) {
            PDFPage *page = [doc pageAtIndex:i];
            NSString *txt = page.string ?: @"";
            [all appendFormat:@"\n===== PAGE %ld =====\n%@\n", (long)(i + 1), txt];
        }

        NSError *err = nil;
        BOOL ok = [all writeToFile:outPath atomically:YES encoding:NSUTF8StringEncoding error:&err];
        if (!ok) {
            fprintf(stderr, "failed to write output: %s\n", err.localizedDescription.UTF8String);
            return 3;
        }
        printf("ok\t%s\tpages=%ld\n", argv[2], (long)doc.pageCount);
    }
    return 0;
}
