module.exports = {
    entry: {
        anomaly: "./anomaly.ts",
        edit_group: "./edit_group.ts",
        home: "./home.ts",
        interaction_graph: "./interaction_graph.ts",
        keyword: "./keyword.ts",
        loading: "./loading.ts",
        news: "./news.ts",
        storify: "./storify.ts",
        top: "./top.ts",
        tweets: "./tweets.ts",
    },
    output: {
        filename: "[name].js",
        path: __dirname + "/static"
    },

    // Enable sourcemaps for debugging webpack's output.
    devtool: "source-map",

    resolve: {
        // Add '.ts' and '.tsx' as resolvable extensions.
        extensions: [".ts", ".tsx", ".js", ".json"]
    },

    module: {
        rules: [
            // All files with a '.ts' or '.tsx' extension will be handled by 'awesome-typescript-loader'.
            { test: /\.tsx?$/, loader: "awesome-typescript-loader" },

            // All output '.js' files will have any sourcemaps re-processed by 'source-map-loader'.
            { enforce: "pre", test: /\.js$/, loader: "source-map-loader" }
        ]
    },

    // When importing a module whose path matches one of the following, just
    // assume a corresponding global variable exists and use that instead.
    // This is important because it allows us to avoid bundling all of our
    // dependencies, which allows browsers to cache those libraries between builds.
    externals: {
        "react": "React",
        "react-dom": "ReactDOM",
        "jquery": "$"
    },
};
