ObjC.import('Vision'); ObjC.import('AppKit'); ObjC.import('Foundation');
function run(argv){
  var outDir = argv[0];
  for (var i=1;i<argv.length;i++){
    var path = argv[i];
    var url = $.NSURL.fileURLWithPath(path);
    var req = $.VNRecognizeTextRequest.alloc.init;
    req.recognitionLevel = 0; // accurate
    req.recognitionLanguages = $(['zh-Hans','en-US']);
    req.usesLanguageCorrection = true;
    var h = $.VNImageRequestHandler.alloc.initWithURLOptions(url, $({}));
    h.performRequestsError($([req]), null);
    var res = req.results; var lines=[];
    for (var j=0;j<res.count;j++){ lines.push(res.objectAtIndex(j).topCandidates(1).objectAtIndex(0).string.js); }
    var name = path.split('/').pop().replace('.png','.txt');
    $(lines.join('\n')).writeToFileAtomicallyEncodingError(outDir+'/'+name, true, 4, null);
  }
}
