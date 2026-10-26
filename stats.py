import hashlib

import polars as pl

df = pl.read_parquet("written_answers.parquet")

# df = df.with_columns(
#     pl.col("questions")
#       .arr.eval(pl.element().str.len_chars())
#       .alias("question_lengths")
# )

# df.select("questions", "question_lengths").head()

df_exploded = (
    df
    .explode("questions")
    .with_columns(
        pl.col("questions")
          .str.len_chars()
          .alias("question_length")
    )
)

df = (
    df
    .with_columns(
        pl.col("answer_clean")
          .str.len_chars()
          .alias("answer_length")
    )
)

df_exploded.head()
df_exploded.select(pl.col("question_length").mean())
df_exploded.select(pl.col("question_length").median())
df.height
df_exploded.height
df.select("answer_clean").head()
df.select(pl.col("answer_length").mean())
df.select(pl.col("answer_length").median())

df = df.with_columns(
    pl.col("questions")
      .list.len()
      .alias("n_questions")
)
df.select("questions", "n_questions").head()
num_questions = df.group_by("n_questions").agg(
    pl.len().alias("n_records")).sort("n_questions")
df.filter(pl.col("n_questions") >= 244).select("id").head()

ids = (
    df
    .filter(pl.col("n_questions") >= 200)
    .select("id")
    .to_series()
    .to_list()
)

print("\n".join(ids))


##num_questions.write_csv("number_of_questions.csv")


# Compute global stats:

# total rows 681424

# total questions 838816
# mean question length 220
# median question length 193
# average answer length 1287
# median answer length 991

# 95th percentile token count

# grouped answer frequency

# def normalize(text):
#     text = text.strip()
#     text = " ".join(text.split())
#     return text

# df_exploded = df_exploded(
# with_columns("questions")
#     normalize(pl.col("questions"))
#     .alias("norm_questions")
# )

# df_exploded = df_exploded.with_columns(
#     pl.col("questions")
#       .str.strip_chars()
#       .str.replace_all(r"\s+", " ")
#       .alias("answer_normalized")
# )

## there may be repitive strings at the end of some questions

# df_exploded = df_exploded.with_columns(
#     pl.col("questions")
#       .str.slice(-100)
#       .alias("last_100_chars")
# )
##df_exploded.select("id", "questions", "last_100_chars").write_csv("last_100.csv")
df_exploded = df_exploded.with_columns(
    pl.col("questions")
      .str.replace_all(
          r"and if\s+(?:he|she)\s+will make a statement on the matter\.",
          ""
      )
      .str.replace_all(r"\s+", " ")   # collapse multiple spaces
      .str.strip_chars()              # trim leading/trailing whitespace
      .alias("questions")
)
##df_exploded.select("questions_cleaned").write_csv("questions.csv")
## clean up and has each question and hash oit so I can dedupe them
df_exploded = df_exploded.with_columns(
    pl.col("questions")
      .str.strip_chars()
      .str.replace_all(r"\s+", " ")
      .hash()
      .cast(pl.Utf8)
      .alias("question_hash")
)
df_exploded.select("question_hash").head()

df_unique_questions = df_exploded.group_by("id").agg(
    pl.col("question_hash")
    .n_unique()
    .alias("questions_unique")
)

#df_unique_questions.head()

df = df.join(
    df_unique_questions,
    on="id",
    how="left"
)

df.select( 'n_questions','questions_unique').head()
questions_u = df.group_by("questions_unique").agg(
    pl.len().alias("u_questions")
).sort("questions_unique")
#questions_u.write_csv("u_questions.csv")

print(df_exploded.columns)
df_unique = df_exploded.unique(
    subset=["id", "question_hash"],
    keep="first"
)
df_unique.columns

df_unique = df_unique\
    .select('id',
            'date',
            'year',
            'house',
            'heading',
            'section_id',
            'department_role',
            'answering_person',
            'answering_role',
            'question_numbers',
            'questions',
            'answer_clean',
            'question_hash'
            )
print(df_unique.height, df_exploded.height)
u_group = df_unique.group_by("id").agg(
    pl.len().alias("counts"),
    pl.col("question_hash").n_unique().alias("distinct")
)

counts = u_group.group_by("counts").agg(
    pl.len().alias("n")
)

dist = u_group.group_by("distinct").agg(
    pl.len().alias("n_distinct")
)
## Table will be empty if all question are distinct
counts.join(
    dist,
    left_on="counts",
    right_on="distinct",
    how="full"
).filter(pl.col("n") != pl.col("n_distinct"))   

## Now I'll re-group the table back into one row per id
df_rebuilt = (
    df_unique
    .group_by("id")
    .agg(
        pl.col("questions").alias("questions"),
        pl.col("question_hash").alias("question_hash")
    )
)

## Ensure the dedupped table hase the same number of rows
## as the original. 
print(df.height, df_rebuilt.height)


df_output = df\
    .select('id',
            'date',
            'year',
            'house',
            'heading',
            'section_id',
            'department_role',
            'answering_person',
            'answering_role',
            'answer_clean')\
    .join(df_rebuilt,
          on="id",
          how="inner")
print(df_output.height)

df_output\
    .with_columns(
        pl.col("answer_clean")
          .str.len_chars()
          .alias("answer_length")
    ).with_columns(
        pl.col("answer_length")
        .quantile(0.95)
        .alias("answer_length_95th"),
        pl.col("answer_length")
        .quantile(0.99)
        .alias("answer_length_99th"),
        pl.col("answer_length")
        .max().alias("answer_length_max")
    ).select("answer_length",
             "answer_length_95th",
             "answer_length_99th",
             "answer_length_max")

