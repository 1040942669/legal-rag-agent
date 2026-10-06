/* Experimental, local-only bridge. No service, provider, or network interface. */
import java.io.BufferedReader;
import java.io.BufferedWriter;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.OutputStreamWriter;
import java.io.PrintWriter;
import java.nio.ByteBuffer;
import java.nio.charset.CharacterCodingException;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.nio.file.FileAlreadyExistsException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.util.ArrayList;
import java.util.Base64;
import java.util.HashSet;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

import org.apache.lucene.analysis.TokenStream;
import org.apache.lucene.analysis.cn.smart.SmartChineseAnalyzer;
import org.apache.lucene.analysis.tokenattributes.CharTermAttribute;
import org.apache.lucene.document.Document;
import org.apache.lucene.document.Field;
import org.apache.lucene.document.SortedDocValuesField;
import org.apache.lucene.document.StoredField;
import org.apache.lucene.document.TextField;
import org.apache.lucene.index.DirectoryReader;
import org.apache.lucene.index.IndexWriter;
import org.apache.lucene.index.IndexWriterConfig;
import org.apache.lucene.index.Term;
import org.apache.lucene.search.BooleanClause;
import org.apache.lucene.search.BooleanQuery;
import org.apache.lucene.search.IndexSearcher;
import org.apache.lucene.search.Query;
import org.apache.lucene.search.ScoreDoc;
import org.apache.lucene.search.Sort;
import org.apache.lucene.search.SortField;
import org.apache.lucene.search.TermQuery;
import org.apache.lucene.search.TopFieldCollector;
import org.apache.lucene.search.TopFieldDocs;
import org.apache.lucene.search.similarities.BM25Similarity;
import org.apache.lucene.store.ByteBuffersDirectory;
import org.apache.lucene.util.BytesRef;

public final class SmartCnBridge {
    private static final String BODY = "body";
    private static final String ID = "id";
    private static final String IDSORT = "idsort";
    private static final int MAX_UNIQUE_QUERY_TERMS = 65_536;
    private static final Base64.Encoder ENCODER = Base64.getEncoder();
    private static final Base64.Decoder DECODER = Base64.getDecoder();

    private static final class ContractError extends Exception {
        final String code;
        ContractError(String code) { this.code = code; }
    }

    private record SourceDocument(String id, String text) { }

    private static String encode(String text) {
        return ENCODER.encodeToString(text.getBytes(StandardCharsets.UTF_8));
    }

    private static String decode(String text) throws ContractError {
        try {
            byte[] bytes = DECODER.decode(text);
            return StandardCharsets.UTF_8.newDecoder()
                .onMalformedInput(CodingErrorAction.REPORT)
                .onUnmappableCharacter(CodingErrorAction.REPORT)
                .decode(ByteBuffer.wrap(bytes)).toString();
        } catch (IllegalArgumentException | CharacterCodingException failure) {
            throw new ContractError("invalid_encoding");
        }
    }

    private static List<SourceDocument> documents(Path path) throws IOException, ContractError {
        List<SourceDocument> documents = new ArrayList<>();
        Set<String> ids = new HashSet<>();
        try (BufferedReader input = Files.newBufferedReader(path, StandardCharsets.UTF_8)) {
            String line;
            while ((line = input.readLine()) != null) {
                String[] cells = line.split("\t", -1);
                if (cells.length != 2) throw new ContractError("invalid_document");
                String id = decode(cells[0]);
                String text = decode(cells[1]);
                if (id.isEmpty()) throw new ContractError("invalid_document");
                if (!ids.add(id)) throw new ContractError("duplicate_id");
                documents.add(new SourceDocument(id, text));
            }
        }
        return documents;
    }

    private static List<String> analyze(SmartChineseAnalyzer analyzer, String text) throws IOException {
        List<String> tokens = new ArrayList<>();
        try (TokenStream stream = analyzer.tokenStream(BODY, text)) {
            CharTermAttribute term = stream.addAttribute(CharTermAttribute.class);
            stream.reset();
            while (stream.incrementToken()) tokens.add(term.toString());
            stream.end();
        }
        return tokens;
    }

    private static List<String> analyzeQuery(SmartChineseAnalyzer analyzer, String text)
            throws IOException, ContractError {
        List<String> tokens = analyze(analyzer, text);
        if (new HashSet<>(tokens).size() > MAX_UNIQUE_QUERY_TERMS) {
            throw new ContractError("too_many_query_terms");
        }
        return tokens;
    }

    private static long[] exportTokens(SmartChineseAnalyzer analyzer, List<SourceDocument> docs,
                                       Path tokenPath) throws IOException, ContractError {
        long totalTokens = 0;
        long nonemptyDocuments = 0;
        try (BufferedWriter output = Files.newBufferedWriter(tokenPath, StandardCharsets.UTF_8,
                StandardOpenOption.CREATE_NEW, StandardOpenOption.WRITE)) {
            for (SourceDocument document : docs) {
                List<String> terms = analyze(analyzer, document.text());
                totalTokens += terms.size();
                if (!terms.isEmpty()) nonemptyDocuments++;
                output.write(encode(document.id()));
                for (String term : terms) {
                    output.write('\t');
                    output.write(encode(term));
                }
                output.newLine();
            }
        } catch (FileAlreadyExistsException failure) {
            throw new ContractError("output_exists");
        }
        return new long[] {totalTokens, nonemptyDocuments};
    }

    private static float parameter(String text, boolean isB) throws ContractError {
        try {
            float value = Float.parseFloat(text);
            if (!Float.isFinite(value) || value < 0 || (isB && value > 1)) {
                throw new ContractError("invalid_parameters");
            }
            return value;
        } catch (NumberFormatException failure) {
            throw new ContractError("invalid_parameters");
        }
    }

    private static int topK(String text) throws ContractError {
        try {
            int value = Integer.parseInt(text);
            if (value < 1) throw new ContractError("invalid_top_k");
            return value;
        } catch (NumberFormatException failure) {
            throw new ContractError("invalid_top_k");
        }
    }

    private static void analysisResponse(PrintWriter output, SmartChineseAnalyzer analyzer,
                                         String encodedText) throws IOException, ContractError {
        String text = decode(encodedText);
        long start = System.nanoTime();
        List<String> terms = analyzeQuery(analyzer, text);
        long elapsed = System.nanoTime() - start;
        StringBuilder frame = new StringBuilder("T\t").append(elapsed);
        for (String term : terms) frame.append('\t').append(encode(term));
        output.println(frame);
    }

    private static void queryResponse(PrintWriter output, SmartChineseAnalyzer analyzer,
            DirectoryReader reader, IndexSearcher searcher, String rawTopK, String encodedQuery)
            throws IOException, ContractError {
        int requested = topK(rawTopK);
        String text = decode(encodedQuery);
        long start = System.nanoTime();
        Set<String> terms = new LinkedHashSet<>(analyzeQuery(analyzer, text));
        List<ScoreDoc> hits = new ArrayList<>();
        if (!terms.isEmpty() && reader.numDocs() > 0) {
            BooleanQuery.Builder queryBuilder = new BooleanQuery.Builder();
            for (String term : terms) {
                queryBuilder.add(new TermQuery(new Term(BODY, term)), BooleanClause.Occur.SHOULD);
            }
            Query query = queryBuilder.build();
            Sort order = new Sort(SortField.FIELD_SCORE, new SortField(IDSORT, SortField.Type.STRING));
            TopFieldDocs top = searcher.search(query, Math.min(requested, reader.numDocs()), order);
            // Field-sorted search does not promise populated ScoreDoc.score values.
            TopFieldCollector.populateScores(top.scoreDocs, searcher, query);
            for (ScoreDoc hit : top.scoreDocs) {
                if (!Float.isFinite(hit.score) || hit.score < 0) throw new ContractError("invalid_score");
                if (hit.score > 0) hits.add(hit);
            }
        }
        long elapsed = System.nanoTime() - start;
        StringBuilder frame = new StringBuilder("R\t").append(elapsed);
        for (ScoreDoc hit : hits) {
            String id = reader.storedFields().document(hit.doc).get(ID);
            if (id == null || id.isEmpty()) throw new ContractError("invalid_identity");
            frame.append('\t').append(encode(id)).append('\t').append(Float.toString(hit.score));
        }
        output.println(frame);
    }

    private static void serve(String mode, SmartChineseAnalyzer analyzer, DirectoryReader reader,
            IndexSearcher searcher, PrintWriter output) throws IOException, ContractError {
        checkOutput(output);
        try (BufferedReader commands = new BufferedReader(new InputStreamReader(System.in,
                StandardCharsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT)
                    .onUnmappableCharacter(CodingErrorAction.REPORT)))) {
            String line;
            while ((line = commands.readLine()) != null) {
                String[] cells = line.split("\t", -1);
                if (cells.length == 1 && cells[0].equals("QUIT")) {
                    output.println("BYE");
                    checkOutput(output);
                    return;
                }
                if (cells.length == 2 && cells[0].equals("A")) {
                    analysisResponse(output, analyzer, cells[1]);
                } else if (cells.length == 3 && cells[0].equals("Q")) {
                    if (!mode.equals("lucene")) throw new ContractError("mode_command");
                    queryResponse(output, analyzer, reader, searcher, cells[1], cells[2]);
                } else {
                    throw new ContractError("invalid_command");
                }
                checkOutput(output);
            }
        }
    }

    private static void checkOutput(PrintWriter output) throws ContractError {
        // System.out is itself a PrintStream and can swallow the pipe's IOException.
        // The outer writer alone therefore cannot detect a disconnected parent.
        if (output.checkError() || System.out.checkError()) {
            throw new ContractError("output_closed");
        }
    }

    private static void run(String[] args, PrintWriter output) throws IOException, ContractError {
        if (args.length != 5 || !(args[0].equals("lucene") || args[0].equals("tokenize"))) {
            throw new ContractError("invalid_arguments");
        }
        float k1 = parameter(args[3], false);
        float b = parameter(args[4], true);
        IndexSearcher.setMaxClauseCount(MAX_UNIQUE_QUERY_TERMS);
        long start = System.nanoTime();
        List<SourceDocument> docs = documents(Path.of(args[1]));
        try (SmartChineseAnalyzer analyzer = new SmartChineseAnalyzer(true)) {
            if (args[0].equals("tokenize")) {
                long[] counts = exportTokens(analyzer, docs, Path.of(args[2]));
                output.println("READY\t" + docs.size() + "\t" + (System.nanoTime() - start)
                    + "\t" + counts[0] + "\t" + counts[1]);
                serve(args[0], analyzer, null, null, output);
            } else {
                BM25Similarity similarity = new BM25Similarity(k1, b, true);
                try (ByteBuffersDirectory directory = new ByteBuffersDirectory()) {
                    IndexWriterConfig config = new IndexWriterConfig(analyzer).setSimilarity(similarity);
                    try (IndexWriter writer = new IndexWriter(directory, config)) {
                        for (SourceDocument source : docs) {
                            Document document = new Document();
                            document.add(new TextField(BODY, source.text(), Field.Store.NO));
                            document.add(new StoredField(ID, source.id()));
                            document.add(new SortedDocValuesField(IDSORT, new BytesRef(source.id())));
                            writer.addDocument(document);
                        }
                    }
                    try (DirectoryReader reader = DirectoryReader.open(directory)) {
                        IndexSearcher searcher = new IndexSearcher(reader);
                        searcher.setSimilarity(similarity);
                        long totalTokens = reader.getSumTotalTermFreq(BODY);
                        int nonemptyDocuments = reader.getDocCount(BODY);
                        if (totalTokens < 0 || nonemptyDocuments < 0) {
                            throw new ContractError("invalid_statistics");
                        }
                        output.println("READY\t" + docs.size() + "\t" + (System.nanoTime() - start)
                            + "\t" + totalTokens + "\t" + nonemptyDocuments);
                        serve(args[0], analyzer, reader, searcher, output);
                    }
                }
            }
        }
    }

    public static void main(String[] args) {
        PrintWriter output = new PrintWriter(new OutputStreamWriter(System.out, StandardCharsets.UTF_8), true);
        int status = 0;
        try {
            run(args, output);
        } catch (ContractError failure) {
            output.println("ERR\t" + failure.code);
            status = 2;
        } catch (CharacterCodingException failure) {
            output.println("ERR\tinvalid_encoding");
            status = 2;
        } catch (IOException failure) {
            output.println("ERR\tio_failure");
            status = 2;
        } catch (RuntimeException failure) {
            output.println("ERR\truntime_failure");
            status = 2;
        }
        output.flush();
        if (status != 0) System.exit(status);
    }
}
